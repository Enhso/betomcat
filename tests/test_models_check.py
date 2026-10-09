"""Tests for `models_check`: pool vs OpenRouter catalog, text-level edits."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
import yaml
from pytest_httpx import HTTPXMock

from betomcat import cli
from betomcat.forecast import REPO_ROOT
from betomcat.models_check import (
    CATALOG_URL,
    MIN_CATALOG_ENTRIES,
    check_pool,
    fetch_catalog,
    run_check,
)
from betomcat.pool import effective_key, load_pool

TODAY = date(2026, 10, 9)
FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_models.json"
REAL_POOL = REPO_ROOT / "config" / "models.yaml"
BODY_FOOTER = (
    "Merge to apply from the next shift. To decline an added model, set its "
    "`enabled: false` in this PR before merging, so it is not proposed again."
)

POOL_TEXT = """\
# test pool header comment
ensemble_width: 2

models:
  # -- funded key, paid ---------------------------------------------------
  - id: anthropic/claude-sonnet-5.5
    tier: frontier
    enabled: true
    key: funded
    max_tokens: 8000
    reasoning:
      effort: high
    price_in: 2
    price_out: 10
    context_tokens: 1000000
    notes: "Primary frontier peer."

  - id: openai/gpt-6-luna
    tier: frontier
    enabled: true
    key: funded
    max_tokens: 8000
    price_in: 0.10
    price_out: 0.50
    context_tokens: 1050000
    notes: null

  # -- free key, free -----------------------------------------------------
  - id: nvidia/nemotron-3-super-120b-a12b:free
    tier: free
    enabled: true
    key: free
    max_tokens: 8000
    price_in: 0
    price_out: 0
    context_tokens: 262144
    notes: "needs a \\"free\\" key; seen 2026-09-30"

  - id: z-ai/glm-5.2:free
    tier: free
    enabled: false
    key: free
    max_tokens: 8000
    price_in: 0
    price_out: 0
    context_tokens: 32000
    notes: "Declined by the owner."
"""
ENABLED_IDS = [
    "anthropic/claude-sonnet-5.5",
    "openai/gpt-6-luna",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


def _entry(
    model_id: str,
    created: str = "2026-09-29",
    *,
    reasoning: bool = True,
    expiration: str | None = None,
    prompt: str = "0.000002",
    completion: str = "0.00001",
    context: int = 1_050_000,
) -> dict[str, Any]:
    released = datetime.fromisoformat(created).replace(tzinfo=UTC)
    return {
        "id": model_id,
        "created": int(released.timestamp()),
        "pricing": {"prompt": prompt, "completion": completion},
        "context_length": context,
        "expiration_date": expiration,
        "supported_parameters": ["max_tokens", *(["reasoning"] if reasoning else [])],
    }


def _catalog(
    *extra: dict[str, Any], without: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """A catalog holding every enabled test-pool id, plus `extra` entries."""
    present = [_entry(i, "2026-09-01") for i in ENABLED_IDS if i not in without]
    return [*present, *extra]


def _write_pool(tmp_path: Path, text: str = POOL_TEXT) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(text, "utf-8")
    return path


def _by_id(path: Path) -> dict[str, Any]:
    return {m.id: m for m in load_pool(path).models}


def _fixture_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = orjson.loads(FIXTURE.read_bytes())["data"]
    return catalog


def test_additions_are_new_openai_anthropic_and_free_models_after_the_floor() -> None:
    catalog = _catalog(
        _entry("openai/gpt-new"),
        _entry("anthropic/claude-new"),
        _entry("vendor/model-new:free", context=262_144),
        _entry("openai/gpt-new:batch"),
        _entry("openai/gpt-old", "2026-09-21"),
        _entry("google/gemini-new"),
        _entry("mistralai/mistral-new"),
        _entry("z-ai/glm-5.2:free"),
    )

    result = check_pool(catalog, POOL_TEXT, TODAY, min_catalog_entries=1)

    assert [a.id for a in result.additions] == [
        "anthropic/claude-new",
        "openai/gpt-new",
        "vendor/model-new:free",
    ]
    assert [a.tier for a in result.additions] == ["frontier", "frontier", "free"]
    assert result.disabled == []


def test_a_model_released_exactly_at_the_floor_is_a_candidate() -> None:
    catalog = _catalog(_entry("openai/gpt-floor", "2026-09-22"))

    result = check_pool(catalog, POOL_TEXT, TODAY, min_catalog_entries=1)

    assert [a.id for a in result.additions] == ["openai/gpt-floor"]


def test_additions_are_appended_in_the_existing_style() -> None:
    catalog = _catalog(
        _entry("openai/gpt-new", prompt="0.000002", completion="0.00001"),
        _entry("vendor/free-new:free", "2026-10-01", prompt="0", completion="0",
               reasoning=False, context=262_144),
    )  # fmt: skip

    result = check_pool(catalog, POOL_TEXT, TODAY, min_catalog_entries=1)

    assert result.pool_text == POOL_TEXT + (
        "\n"
        "  - id: openai/gpt-new\n"
        "    tier: frontier\n"
        "    enabled: true\n"
        "    max_tokens: 8000\n"
        "    reasoning:\n"
        "      effort: high\n"
        "    price_in: 2\n"
        "    price_out: 10\n"
        "    context_tokens: 1050000\n"
        '    notes: "Added by the weekly model check on 2026-10-09 '
        '(released 2026-09-29); not probed yet."\n'
        "\n"
        "  - id: vendor/free-new:free\n"
        "    tier: free\n"
        "    enabled: true\n"
        "    max_tokens: 8000\n"
        "    price_in: 0\n"
        "    price_out: 0\n"
        "    context_tokens: 262144\n"
        '    notes: "Added by the weekly model check on 2026-10-09 '
        '(released 2026-10-01); not probed yet."\n'
    )


def test_edited_pool_still_loads_and_routes_added_ids(tmp_path: Path) -> None:
    pool_path = _write_pool(tmp_path)
    catalog = _catalog(
        _entry("openai/gpt-new", prompt="0.00000068", completion="0.00000209"),
        _entry("vendor/free-new:free", prompt="0", completion="0"),
    )

    result = run_check(pool_path, None, catalog, TODAY, min_catalog_entries=1)

    assert result.pool_text.startswith(POOL_TEXT)
    models = _by_id(pool_path)
    paid, free = models["openai/gpt-new"], models["vendor/free-new:free"]
    assert (paid.enabled, paid.tier, paid.max_tokens) == (True, "frontier", 8000)
    assert (paid.price_in, paid.price_out) == (0.68, 2.09)
    assert paid.reasoning == {"effort": "high"}
    assert paid.key is None
    assert (effective_key(paid), effective_key(free)) == ("funded", "free")
    assert (free.enabled, free.tier) == (True, "free")
    assert len(models) == len(yaml.safe_load(POOL_TEXT)["models"]) + 2


def test_reasoning_block_is_omitted_when_unsupported(tmp_path: Path) -> None:
    pool_path = _write_pool(tmp_path)
    catalog = _catalog(_entry("openai/gpt-plain", reasoning=False))

    result = run_check(pool_path, None, catalog, TODAY, min_catalog_entries=1)

    assert "reasoning" not in result.pool_text[len(POOL_TEXT) :]
    assert _by_id(pool_path)["openai/gpt-plain"].reasoning is None


def test_entry_missing_from_catalog_is_disabled_with_a_note(tmp_path: Path) -> None:
    pool_path = _write_pool(tmp_path)
    catalog = _catalog(without=("anthropic/claude-sonnet-5.5",))

    result = run_check(pool_path, None, catalog, TODAY, min_catalog_entries=1)

    assert [(d.id, d.reason) for d in result.disabled] == [
        ("anthropic/claude-sonnet-5.5", "missing from OpenRouter's catalog")
    ]
    old_lines = POOL_TEXT.split("\n")
    new_lines = pool_path.read_text("utf-8").split("\n")
    assert len(new_lines) == len(old_lines)
    changed = {
        i: (o, n)
        for i, (o, n) in enumerate(zip(old_lines, new_lines, strict=True))
        if o != n
    }
    assert len(changed) == 2
    enabled_line, notes_line = changed.values()
    assert enabled_line == ("    enabled: true", "    enabled: false")
    assert notes_line[0] == '    notes: "Primary frontier peer."'
    assert notes_line[1] == (
        '    notes: "Primary frontier peer. Disabled 2026-10-09 by the weekly '
        "model check: missing from OpenRouter's catalog.\""
    )
    model = _by_id(pool_path)["anthropic/claude-sonnet-5.5"]
    assert model.enabled is False
    assert all(m.enabled for i, m in _by_id(pool_path).items() if i in ENABLED_IDS[1:])


def test_null_note_is_replaced_by_just_the_disabled_sentence(tmp_path: Path) -> None:
    pool_path = _write_pool(tmp_path)
    catalog = _catalog(without=("openai/gpt-6-luna",))

    run_check(pool_path, None, catalog, TODAY, min_catalog_entries=1)

    assert _by_id(pool_path)["openai/gpt-6-luna"].notes == (
        "Disabled 2026-10-09 by the weekly model check: "
        "missing from OpenRouter's catalog."
    )


def test_old_note_with_double_quotes_survives(tmp_path: Path) -> None:
    pool_path = _write_pool(tmp_path)
    catalog = _catalog(without=("nvidia/nemotron-3-super-120b-a12b:free",))

    run_check(pool_path, None, catalog, TODAY, min_catalog_entries=1)

    note = _by_id(pool_path)["nvidia/nemotron-3-super-120b-a12b:free"].notes
    assert note is not None
    assert note.startswith('needs a "free" key; seen 2026-09-30. Disabled 2026-10-09')


def test_entry_without_a_notes_line_gets_one(tmp_path: Path) -> None:
    text = POOL_TEXT.replace('    notes: "Primary frontier peer."\n', "")
    pool_path = _write_pool(tmp_path, text)
    catalog = _catalog(without=("anthropic/claude-sonnet-5.5",))

    run_check(pool_path, None, catalog, TODAY, min_catalog_entries=1)

    model = _by_id(pool_path)["anthropic/claude-sonnet-5.5"]
    assert model.enabled is False
    assert model.notes is not None
    assert model.notes.startswith("Disabled 2026-10-09")


def test_entry_that_cannot_be_edited_raises() -> None:
    text = POOL_TEXT.replace("enabled: true", "enabled: yes", 1)
    catalog = _catalog(without=("anthropic/claude-sonnet-5.5",))

    with pytest.raises(ValueError, match="enabled: true"):
        check_pool(catalog, text, TODAY, min_catalog_entries=1)


def test_disabled_pool_entries_are_neither_disabled_again_nor_re_added() -> None:
    catalog = _catalog(_entry("z-ai/glm-5.2:free"))

    result = check_pool(catalog, POOL_TEXT, TODAY, min_catalog_entries=1)

    assert result.additions == []
    assert result.disabled == []
    assert result.pool_text == POOL_TEXT


@pytest.mark.parametrize(
    ("expiration", "disabled"),
    [
        ("2026-10-16", True),
        ("2026-10-10", True),
        ("2026-10-08", True),
        ("2026-10-17", False),
    ],
)
def test_expiration_within_seven_days_disables_later_is_only_reported(
    expiration: str, disabled: bool
) -> None:
    catalog = [
        *_catalog(without=("openai/gpt-6-luna",)),
        _entry("openai/gpt-6-luna", expiration=expiration),
    ]

    result = check_pool(catalog, POOL_TEXT, TODAY, min_catalog_entries=1)

    if disabled:
        assert [(d.id, d.reason) for d in result.disabled] == [
            ("openai/gpt-6-luna", f"OpenRouter expiration_date {expiration}")
        ]
        assert result.expiring_later == []
        assert result.changed
    else:
        assert result.disabled == []
        assert [(e.id, str(e.expires)) for e in result.expiring_later] == [
            ("openai/gpt-6-luna", expiration)
        ]
        assert result.pool_text == POOL_TEXT


def test_expiration_of_a_disabled_pool_entry_is_ignored() -> None:
    catalog = _catalog(_entry("z-ai/glm-5.2:free", expiration="2026-10-10"))

    result = check_pool(catalog, POOL_TEXT, TODAY, min_catalog_entries=1)

    assert result.disabled == []
    assert result.expiring_later == []


def test_no_changes_leaves_the_file_untouched_and_reports_zeroes(
    tmp_path: Path,
) -> None:
    pool_path = _write_pool(tmp_path)
    mtime_before = pool_path.stat().st_mtime_ns
    body_path = tmp_path / "body.md"

    result = run_check(pool_path, body_path, _catalog(), TODAY, min_catalog_entries=1)

    assert not result.changed
    assert result.summary == "added 0, disabled 0, expiring later 0"
    assert pool_path.read_text("utf-8") == POOL_TEXT
    assert pool_path.stat().st_mtime_ns == mtime_before
    body = body_path.read_text("utf-8")
    assert body.count("none") == 3
    assert body.rstrip().endswith(BODY_FOOTER)


def test_body_lists_every_section(tmp_path: Path) -> None:
    catalog = [
        *_catalog(without=("anthropic/claude-sonnet-5.5", "openai/gpt-6-luna")),
        _entry("openai/gpt-6-luna", expiration="2026-12-31"),
        _entry("openai/gpt-new", prompt="0.00000068", completion="0.00000209"),
    ]
    body_path = tmp_path / "body.md"

    result = run_check(
        _write_pool(tmp_path), body_path, catalog, TODAY, min_catalog_entries=1
    )

    assert result.summary == "added 1, disabled 1, expiring later 1"
    body = body_path.read_text("utf-8")
    assert "| `openai/gpt-new` | 0.68 | 2.09 | 1,050,000 | 2026-09-29 |" in body
    assert "- `anthropic/claude-sonnet-5.5`: missing from OpenRouter's catalog" in body
    assert "- `openai/gpt-6-luna`: 2026-12-31" in body
    assert "none" not in body
    assert body.rstrip().endswith(BODY_FOOTER)


def test_catalog_that_is_too_small_raises_and_changes_nothing(tmp_path: Path) -> None:
    pool_path = _write_pool(tmp_path)
    body_path = tmp_path / "body.md"
    mtime_before = pool_path.stat().st_mtime_ns

    with pytest.raises(ValueError, match=f"fewer than the {MIN_CATALOG_ENTRIES}"):
        run_check(pool_path, body_path, _fixture_catalog(), TODAY)

    assert pool_path.read_text("utf-8") == POOL_TEXT
    assert pool_path.stat().st_mtime_ns == mtime_before
    assert not body_path.exists()


def test_fixture_against_the_real_pool(tmp_path: Path) -> None:
    pool_path = tmp_path / "models.yaml"
    shutil.copyfile(REAL_POOL, pool_path)
    original = pool_path.read_text("utf-8")
    body_path = tmp_path / "body.md"

    result = run_check(
        pool_path,
        body_path,
        _fixture_catalog(),
        date(2026, 10, 9),
        min_catalog_entries=1,
    )

    assert [a.id for a in result.additions] == [
        "apodex/apodex-1.1-mini:free",
        "openai/gpt-6.1-sol",
        "openai/gpt-6.1-sol-pro",
    ]
    assert [(e.id, str(e.expires)) for e in result.expiring_later] == [
        ("dots-studio/dots-3-note-preview:free", "2026-12-31")
    ]
    assert "dots-studio/dots-3-note-preview:free" not in [d.id for d in result.disabled]
    assert (
        "### Expiring later\n\n- `dots-studio/dots-3-note-preview:free`: 2026-12-31"
        in (body_path.read_text("utf-8"))
    )
    pool = {m.id: m for m in load_pool(pool_path).models}
    assert pool["dots-studio/dots-3-note-preview:free"].enabled is True
    assert pool["openai/gpt-6.1-sol"].enabled is True
    assert pool["openai/gpt-6.1-sol"].price_out == 10
    assert pool_path.read_text("utf-8") != original


def test_fetch_catalog_returns_the_data_list(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=CATALOG_URL, json={"data": [{"id": "openai/gpt-x"}]})

    assert fetch_catalog() == [{"id": "openai/gpt-x"}]

    request = httpx_mock.get_requests()[0]
    assert request.extensions["timeout"]["read"] == 60.0
    assert "authorization" not in request.headers


def test_cli_check_models_wires_the_options_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pool_path = _write_pool(tmp_path)
    body_path = tmp_path / "body.md"
    padding = [
        _entry(f"filler/model-{i}", "2026-01-01") for i in range(MIN_CATALOG_ENTRIES)
    ]
    catalog = [*padding, *_catalog(_entry("openai/gpt-new"))]
    monkeypatch.setattr("betomcat.models_check.fetch_catalog", lambda: catalog)

    exit_code = cli.main(
        ["check-models", "--pool", str(pool_path), "--body-out", str(body_path)]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "added 1, disabled 0, expiring later 0"
    assert "openai/gpt-new" in pool_path.read_text("utf-8")
    assert "openai/gpt-new" in body_path.read_text("utf-8")
