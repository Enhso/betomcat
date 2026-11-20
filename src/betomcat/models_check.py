"""Weekly check of the model pool against OpenRouter's public catalog.

`betomcat check-models` compares `config/models.yaml` with the catalog at
OpenRouter's /api/v1/models. New OpenAI/Anthropic releases and `:free` models
are appended to the pool as enabled entries; enabled entries that vanished
from the catalog or expire within a week are disabled. The weekly workflow
(.github/workflows/models.yml) puts the result in a pull request, so nothing
reaches main until the owner merges.

The pool file is edited as text so comments and formatting survive exactly;
the parsed YAML is only read for ids, `enabled` flags and old notes.

Free models the check would leave enabled get one test call through the bot's
own client; HTTP 403 and 404 mean the model is dead for us, so those are
disabled too. The calls live here as `probe_free_models`; `check_pool` stays
pure and receives the refusals as an argument.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson
import yaml

from betomcat.llm import LLMError, OpenRouterClient
from betomcat.pool import ModelSpec, parse_pool

logger = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TIMEOUT_SECONDS = 60.0
MIN_CATALOG_ENTRIES = 100
# The day the pool was assembled; every model older than this was considered then.
CANDIDATE_FLOOR = datetime(2026, 9, 22, tzinfo=UTC)
EXPIRY_WINDOW = timedelta(days=7)
PROBE_PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT_SECONDS = 120.0
REFUSED_STATUSES = frozenset({403, 404})

_BLOCK_START = re.compile(r"^(?=  - id: )", re.MULTILINE)
_ID_LINE = re.compile(r"  - id: (\S+)")
_ENABLED_LINE = "    enabled: true"
_NOTES_PREFIX = "    notes:"


@dataclass(frozen=True)
class Addition:
    """A catalog model proposed for the pool."""

    id: str
    tier: str
    reasoning: bool
    price_in: float
    price_out: float
    context_tokens: int
    released: date


@dataclass(frozen=True)
class Disabling:
    """An enabled pool entry the check turns off, with the reason."""

    id: str
    reason: str


@dataclass(frozen=True)
class Expiring:
    """An enabled pool entry whose OpenRouter expiration is not yet near."""

    id: str
    expires: date


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check: the proposed pool text and what changed in it."""

    pool_text: str
    changed: bool
    additions: list[Addition]
    disabled: list[Disabling]
    expiring_later: list[Expiring]

    @property
    def summary(self) -> str:
        """One-line count of each kind of proposed change."""
        return (
            f"added {len(self.additions)}, disabled {len(self.disabled)}, "
            f"expiring later {len(self.expiring_later)}"
        )

    @property
    def body(self) -> str:
        """Markdown pull request body listing every change."""
        added = [
            f"| `{a.id}` | {a.price_in:g} | {a.price_out:g} | "
            f"{a.context_tokens:,} | {a.released} |"
            for a in self.additions
        ]
        if added:
            added = [
                "| id | in $/M | out $/M | context | released |",
                "|---|---|---|---|---|",
                *added,
            ]
        disabled = [f"- `{d.id}`: {d.reason}" for d in self.disabled]
        expiring = [f"- `{e.id}`: {e.expires}" for e in self.expiring_later]
        return (
            "Weekly comparison of OpenRouter's model catalog with "
            "`config/models.yaml`.\n\n"
            f"### Added\n\n{_lines_or_none(added)}\n\n"
            f"### Disabled\n\n{_lines_or_none(disabled)}\n\n"
            f"### Expiring later\n\n{_lines_or_none(expiring)}\n\n"
            "Merge to apply from the next shift. To decline an added model, set "
            "its `enabled: false` in this PR before merging, so it is not "
            "proposed again.\n"
        )


def _lines_or_none(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "none"


def fetch_catalog() -> list[dict[str, Any]]:
    """Download OpenRouter's public model catalog (no auth needed).

    Returns:
        The `data` list of the /api/v1/models response.

    Raises:
        httpx.HTTPError: On a network failure or a non-2xx response.
    """
    response = httpx.get(CATALOG_URL, timeout=CATALOG_TIMEOUT_SECONDS)
    response.raise_for_status()
    catalog: list[dict[str, Any]] = orjson.loads(response.content)["data"]
    return catalog


def _is_candidate_id(model_id: str) -> bool:
    if model_id.endswith(":free"):
        return True
    return model_id.startswith(("openai/", "anthropic/")) and ":" not in model_id


def find_additions(
    catalog: list[dict[str, Any]], known_ids: set[str]
) -> list[Addition]:
    """Pick the catalog models worth proposing, sorted by id.

    A candidate is an `openai/` or `anthropic/` model without a variant suffix
    or any `:free` model, released on or after `CANDIDATE_FLOOR`, and not
    already in the pool (enabled or disabled).

    Args:
        catalog: Parsed catalog entries.
        known_ids: Every id already in the pool file.

    Returns:
        The proposed additions.
    """
    floor = CANDIDATE_FLOOR.timestamp()
    additions: list[Addition] = []
    for entry in sorted(catalog, key=lambda e: str(e["id"])):
        model_id = entry["id"]
        if (
            not _is_candidate_id(model_id)
            or entry["created"] < floor
            or model_id in known_ids
        ):
            continue
        pricing = entry["pricing"]
        additions.append(
            Addition(
                id=model_id,
                tier="free" if model_id.endswith(":free") else "frontier",
                reasoning="reasoning" in entry["supported_parameters"],
                price_in=float(pricing["prompt"]) * 1_000_000,
                price_out=float(pricing["completion"]) * 1_000_000,
                context_tokens=int(entry["context_length"]),
                released=datetime.fromtimestamp(entry["created"], UTC).date(),
            )
        )
    return additions


def find_removals(
    catalog: list[dict[str, Any]], enabled_ids: list[str], today: date
) -> tuple[list[Disabling], list[Expiring]]:
    """Split the enabled pool entries into those to disable and those to watch.

    Args:
        catalog: Parsed catalog entries.
        enabled_ids: Ids of the pool entries with `enabled: true`.
        today: The date the check runs on.

    Returns:
        Entries to disable (absent from the catalog, or expiring within
        `EXPIRY_WINDOW`) and entries whose expiration is further out.
    """
    by_id = {entry["id"]: entry for entry in catalog}
    cutoff = today + EXPIRY_WINDOW
    disabled: list[Disabling] = []
    expiring_later: list[Expiring] = []
    for model_id in enabled_ids:
        entry = by_id.get(model_id)
        if entry is None:
            disabled.append(Disabling(model_id, "missing from OpenRouter's catalog"))
            continue
        raw_expiry = entry.get("expiration_date")
        if raw_expiry is None:
            continue
        expires = date.fromisoformat(raw_expiry[:10])
        if expires <= cutoff:
            disabled.append(
                Disabling(model_id, f"OpenRouter expiration_date {expires}")
            )
        else:
            expiring_later.append(Expiring(model_id, expires))
    return disabled, expiring_later


def _disabled_note(old_note: str | None, reason: str, today: date) -> str:
    sentence = f"Disabled {today} by the weekly model check: {reason}."
    if not old_note:
        return sentence
    separator = " " if old_note.endswith(".") else ". "
    return f"{old_note}{separator}{sentence}"


def _disable_block(block: str, note: str) -> str:
    """Flip `enabled` and replace (or add) the `notes` line of one entry block."""
    lines = block.split("\n")
    enabled_at = next(
        (i for i, line in enumerate(lines) if line.rstrip() == _ENABLED_LINE), None
    )
    if enabled_at is None:
        raise ValueError(f"no '{_ENABLED_LINE.strip()}' line in block {lines[0]!r}")
    notes_line = f"{_NOTES_PREFIX} {orjson.dumps(note).decode()}"
    notes_at = next(
        (i for i, line in enumerate(lines) if line.startswith(_NOTES_PREFIX)), None
    )
    lines[enabled_at] = "    enabled: false"
    if notes_at is None:
        lines.insert(enabled_at + 1, notes_line)
    else:
        lines[notes_at] = notes_line
    return "\n".join(lines)


def _disable_entries(text: str, notes: dict[str, str]) -> str:
    """Disable the entries named in `notes` (id -> new note), editing only them."""
    blocks = _BLOCK_START.split(text)
    applied = 0
    for i, block in enumerate(blocks):
        match = _ID_LINE.match(block)
        if match and match.group(1) in notes:
            blocks[i] = _disable_block(block, notes[match.group(1)])
            applied += 1
    if applied != len(notes):
        raise ValueError(f"found {applied} of {len(notes)} entries to disable")
    return "".join(blocks)


def _render_addition(
    addition: Addition, today: date, refused_reason: str | None, probed: bool
) -> str:
    note = f"Added by the weekly model check on {today} (released {addition.released})"
    if refused_reason is not None:
        note = _disabled_note(f"{note}.", refused_reason, today)
    else:
        note += f"; {'test call not refused' if probed else 'not probed yet'}."
    lines = [
        "",
        f"  - id: {addition.id}",
        f"    tier: {addition.tier}",
        "    enabled: true",
        "    max_tokens: 8000",
    ]
    if addition.reasoning:
        lines += ["    reasoning:", "      effort: high"]
    lines += [
        f"    price_in: {addition.price_in:g}",
        f"    price_out: {addition.price_out:g}",
        f"    context_tokens: {addition.context_tokens}",
        f"{_NOTES_PREFIX} {orjson.dumps(note).decode()}",
    ]
    block = "\n".join(lines) + "\n"
    return block if refused_reason is None else _disable_block(block, note)


def check_pool(
    catalog: list[dict[str, Any]],
    pool_text: str,
    today: date,
    min_catalog_entries: int = MIN_CATALOG_ENTRIES,
    refused: Mapping[str, str] | None = None,
) -> CheckResult:
    """Compute the proposed pool text for one check; pure, no I/O.

    Args:
        catalog: Parsed catalog entries.
        pool_text: Current contents of `config/models.yaml`.
        today: The date the check runs on.
        min_catalog_entries: Smallest catalog trusted as complete.
        refused: Free model id -> reason, for models a test call was refused
            on. `None` means no test calls were made, so free additions are
            noted as not probed.

    Returns:
        The proposed text plus the additions, disablings and expiry watch list.

    Raises:
        ValueError: If the catalog has fewer than `min_catalog_entries` entries
            (a truncated catalog would mark the whole pool as missing), or an
            entry to disable has no `enabled: true` line.
    """
    if len(catalog) < min_catalog_entries:
        raise ValueError(
            f"catalog has {len(catalog)} entries, fewer than the "
            f"{min_catalog_entries} needed to trust it; pool left untouched"
        )
    entries: list[dict[str, Any]] = yaml.safe_load(pool_text)["models"]
    enabled_ids = [e["id"] for e in entries if e["enabled"]]
    disabled, expiring_later = find_removals(catalog, enabled_ids, today)
    probed = refused is not None
    refused = refused or {}
    already = {d.id for d in disabled}
    disabled += [
        Disabling(i, refused[i])
        for i in enabled_ids
        if i in refused and i not in already
    ]
    additions = find_additions(catalog, {e["id"] for e in entries})
    old_notes = {e["id"]: e.get("notes") for e in entries}
    new_notes = {
        d.id: _disabled_note(old_notes[d.id], d.reason, today) for d in disabled
    }
    text = _disable_entries(pool_text, new_notes)
    if additions:
        text = text if text.endswith("\n") else text + "\n"
        text += "".join(
            _render_addition(
                a, today, refused.get(a.id), probed=probed and a.tier == "free"
            )
            for a in additions
        )
        disabled += [
            Disabling(a.id, refused[a.id]) for a in additions if a.id in refused
        ]
    return CheckResult(text, text != pool_text, additions, disabled, expiring_later)


def free_models_to_probe(
    catalog: list[dict[str, Any]],
    pool_text: str,
    today: date,
    min_catalog_entries: int = MIN_CATALOG_ENTRIES,
) -> list[ModelSpec]:
    """The `:free` models a check would leave enabled, as the bot would load them.

    That is the enabled free entries the check does not disable plus the free
    additions, the latter parsed from the block the check would render.

    Args:
        catalog: Parsed catalog entries.
        pool_text: Current contents of `config/models.yaml`.
        today: The date the check runs on.
        min_catalog_entries: Smallest catalog trusted as complete.

    Returns:
        One spec per model to test-call.

    Raises:
        ValueError: As `check_pool`.
    """
    proposed = check_pool(catalog, pool_text, today, min_catalog_entries).pool_text
    return [m for m in parse_pool(proposed).enabled_models if m.id.endswith(":free")]


async def probe_free_models(
    client: OpenRouterClient, models: list[ModelSpec]
) -> dict[str, str]:
    """Send one tiny request per model; report those OpenRouter refused.

    Calls run one after another to stay clear of the free key's rate limit.
    Only HTTP 403 and 404 count as refused; 429, 5xx, timeouts and empty
    replies are transient and ignored.

    Args:
        client: The bot's own OpenRouter client.
        models: Entries to call, each with its own request parameters.

    Returns:
        Refused model id -> reason.
    """
    refused: dict[str, str] = {}
    for model in models:
        try:
            await client.complete(model, PROBE_PROMPT, PROBE_TIMEOUT_SECONDS)
        except LLMError as exc:
            if exc.status_code in REFUSED_STATUSES:
                refused[model.id] = f"test call refused with HTTP {exc.status_code}"
            else:
                logger.warning("test call to %s inconclusive: %s", model.id, exc)
    return refused


def run_check(
    pool_path: Path,
    body_out: Path | None,
    catalog: list[dict[str, Any]],
    today: date,
    min_catalog_entries: int = MIN_CATALOG_ENTRIES,
    refused: Mapping[str, str] | None = None,
) -> CheckResult:
    """Check the pool file against the catalog, rewriting it only on change.

    Args:
        pool_path: The pool YAML to check and, if needed, rewrite in place.
        body_out: Where to write the Markdown PR body (always written when set).
        catalog: Parsed catalog entries.
        today: The date the check runs on.
        min_catalog_entries: Smallest catalog trusted as complete.
        refused: Test-call refusals, as for `check_pool`.

    Returns:
        The check result.

    Raises:
        ValueError: As `check_pool`; nothing is written in that case.
    """
    result = check_pool(
        catalog, pool_path.read_text("utf-8"), today, min_catalog_entries, refused
    )
    if result.changed:
        pool_path.write_text(result.pool_text, "utf-8")
    if body_out is not None:
        body_out.write_text(result.body, "utf-8")
    return result
