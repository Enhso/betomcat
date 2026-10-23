"""Daily model-weight update (spec s5, HANDOFF 5.3).

Each resolved question is fetched once from Metaculus, cached in the ledger's
`resolutions` table, and every model's own last `ok` forecast per run is
scored against it into `model_scores`.

The score is peer-style. The bot token cannot see the community prediction,
but Metaculus reports the bot's own `spot_peer_score` on each resolved
question. With m_bot the mass the bot's last forecast put on what happened,
the field's mean log score is F = ln(m_bot) - spot_peer_score / 100 (/ 50 on
continuous questions, whose peer score Metaculus halves), cached at fetch
time. A model's score is ln(m_model) - F, clipped to [-5, 5], with
method `peer`. When the post carries no peer score, or the bot's forecast
does not fit the question, the score is ln(m_model) minus a uniform
forecast's, with method `baseline`.

Per-model scores become `DATA_DIR/weights.json` through a shrunk mean and a
temperature softmax. The Actions logs are public, so this module logs counts
only: never a probability, a per-question score or a rationale.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

import httpx
import orjson

from betomcat.ledger import Ledger
from betomcat.pool import load_pool, write_weights

logger = logging.getLogger(__name__)

MIN_SCORED_QUESTIONS = 10
SHRINKAGE_PRIOR = 10
TEMPERATURE = 0.2
WEIGHT_FLOOR = 0.5
MASS_FLOOR = 1e-4
SCORE_CLIP = 5.0
REFETCH_AFTER = timedelta(days=7)
REQUEST_PAUSE_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30.0
API_BASE = "https://www.metaculus.com/api"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

BELOW_LOWER_BOUND = "below_lower_bound"
ABOVE_UPPER_BOUND = "above_upper_bound"
_POST_ID_RE = re.compile(r"/questions/(\d+)")

Forecast = float | dict[str, float] | list[float]


class Scaling(TypedDict):
    """What a cached question needs to turn a forecast into an outcome mass."""

    type: str
    options: list[str] | None
    grid: list[float] | None
    open_lower: bool
    open_upper: bool


@dataclass(frozen=True)
class UpdateSummary:
    """Counts from one `update_weights` run. Safe to log: no forecast content."""

    fetched: int
    scored: int
    by_method: dict[str, int]
    skipped: dict[str, int]
    weights: dict[str, float]
    n_scored: dict[str, int]


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def _to_number(value: str | float) -> float:
    """A resolution or grid value as a float: a decimal, or an ISO date as epoch.

    Raises:
        ValueError: If `value` is neither a number nor an ISO 8601 timestamp.
    """
    try:
        return float(value)
    except ValueError:
        return _parse_time(str(value)).timestamp()


def _scaling_of(question: dict[str, Any]) -> Scaling:
    """Extract a question's `Scaling`, numbering ISO-date grid points as epoch."""
    raw_grid = (question.get("scaling") or {}).get("continuous_range")
    return {
        "type": question["type"],
        "options": question.get("options"),
        "grid": None if raw_grid is None else [_to_number(x) for x in raw_grid],
        "open_lower": bool(question.get("open_lower_bound")),
        "open_upper": bool(question.get("open_upper_bound")),
    }


def _cells(scaling: Scaling) -> int | None:
    """How many outcomes a uniform forecast spreads its mass over."""
    if scaling["type"] == "binary":
        return 2
    if scaling["type"] == "multiple_choice":
        return len(scaling["options"] or []) or None
    grid = scaling["grid"]
    if grid is None:
        return None
    return len(grid) - 1 + scaling["open_lower"] + scaling["open_upper"]


def outcome_mass(forecast: Forecast, resolution: str, scaling: Scaling) -> float | None:
    """Probability `forecast` gave to what actually happened.

    Binary: P(yes) or its complement. Multiple choice: the resolved option's
    probability. Numeric, discrete and date: the mass of the CDF bin holding
    the resolution (found by bisecting the question's grid), or the matching
    tail mass when the resolution lies outside the range.

    Returns:
        The probability, or `None` when `forecast` does not fit this question
        (wrong shape, wrong CDF length, resolution not among the options).

    Raises:
        ValueError: If a continuous resolution is not a number or ISO date.
    """
    kind = scaling["type"]
    if kind == "binary":
        if not isinstance(forecast, float | int) or resolution not in ("yes", "no"):
            return None
        return float(forecast) if resolution == "yes" else 1.0 - float(forecast)
    if kind == "multiple_choice":
        return forecast.get(resolution) if isinstance(forecast, dict) else None
    grid = scaling["grid"]
    if not isinstance(forecast, list) or grid is None or len(forecast) != len(grid):
        return None
    if resolution == BELOW_LOWER_BOUND:
        return forecast[0]
    if resolution == ABOVE_UPPER_BOUND:
        return 1.0 - forecast[-1]
    value = _to_number(resolution)
    if value < grid[0]:
        return forecast[0]
    if value > grid[-1]:
        return 1.0 - forecast[-1]
    index = min(bisect_right(grid, value) - 1, len(grid) - 2)
    return forecast[index + 1] - forecast[index]


def _log_mass(mass: float) -> float:
    return math.log(max(mass, MASS_FLOOR))


def relative_score(
    forecast: Forecast,
    field_log_score: float | None,
    resolution: str,
    scaling: Scaling,
) -> tuple[float, str] | None:
    """A forecast's log score minus the field's (or a uniform forecast's), clipped.

    Returns:
        `(score, method)` with `method` `"peer"` when `field_log_score` is
        given, else `"baseline"`; or `None` when the forecast (or the cell
        count for the baseline) does not fit.
    """
    mass = outcome_mass(forecast, resolution, scaling)
    if mass is None:
        return None
    if field_log_score is not None:
        reference, method = field_log_score, "peer"
    else:
        cells = _cells(scaling)
        if cells is None:
            return None
        reference, method = -math.log(cells), "baseline"
    score = max(-SCORE_CLIP, min(SCORE_CLIP, _log_mass(mass) - reference))
    return score, method


def _forecast_from_values(values: list[float], scaling: Scaling) -> Forecast | None:
    """A Metaculus `forecast_values` list as the shape `outcome_mass` reads.

    Binary lists are `[P(no), P(yes)]`, multiple choice lists follow `options`,
    and continuous lists are the CDF. `None` if the list cannot be the
    question's.
    """
    if scaling["type"] == "binary":
        return float(values[1]) if len(values) == 2 else None
    if scaling["type"] == "multiple_choice":
        options = scaling["options"] or []
        return (
            dict(zip(options, values, strict=True))
            if len(values) == len(options)
            else None
        )
    return list(values)


def _field_log_score(
    question: dict[str, Any], scaling: Scaling, resolution: str
) -> float | None:
    """The field's mean log score, backed out of the bot's own peer score.

    Metaculus scores it as 100 * N/(N-1) * ln(m_bot / geometric mean of all
    forecasters' masses), halved on continuous questions (`scoring/score_math.py`
    in Metaculus's repository), which is 100 * (ln(m_bot) - F) with F the other
    forecasters' mean log mass. So F = ln(m_bot) - score / 100 (/ 50 when
    continuous), with m_bot the mass `my_forecasts.latest.forecast_values` put
    on the outcome. `None` when the post has no peer score or the bot's forecast
    cannot be scored.
    """
    mine = question.get("my_forecasts") or {}
    peer_score = (mine.get("score_data") or {}).get("spot_peer_score")
    values = (mine.get("latest") or {}).get("forecast_values")
    if peer_score is None or not values:
        return None
    bot = _forecast_from_values(values, scaling)
    try:
        mass = None if bot is None else outcome_mass(bot, resolution, scaling)
    except ValueError:
        return None
    if mass is None:
        return None
    scale = 100 if scaling["type"] in ("binary", "multiple_choice") else 50
    return _log_mass(mass) - float(peer_score) / scale


def parse_post(post: dict[str, Any], question_id: str) -> dict[str, Any]:
    """Reduce an API post to the `resolutions` row fields.

    `status` is `resolved` (scorable), `annulled` (the API says annulled or
    ambiguous) or `pending` (anything else, including a resolved post whose
    resolution the API leaves null).
    """
    question = post.get("question")
    if not question or f"metaculus:{question.get('id')}" != question_id:
        return {
            "status": "pending",
            "resolution": None,
            "field_log_score": None,
            "scaling": None,
            "scheduled_resolve_time": None,
        }
    scaling = _scaling_of(question)
    resolution = question.get("resolution")
    if question.get("status") != "resolved" or resolution is None:
        status = "pending"
    elif resolution in ("annulled", "ambiguous"):
        status = "annulled"
    else:
        status = "resolved"
    return {
        "status": status,
        "resolution": resolution,
        "field_log_score": (
            _field_log_score(question, scaling, resolution)
            if status == "resolved"
            else None
        ),
        "scaling": scaling,
        "scheduled_resolve_time": question.get("scheduled_resolve_time"),
    }


def _needs_fetch(
    question: dict[str, Any], cached: dict[str, Any] | None, now: datetime
) -> bool:
    """Fetch once after close. Refetch a pending question, or a resolved one
    still lacking the field's score, after its scheduled resolve time passes or
    7 days after the last fetch. Resolved with a field score, or annulled, is final."""
    if cached is None:
        return (
            question["close_time"] is None or _parse_time(question["close_time"]) <= now
        )
    if cached["status"] == "annulled" or (
        cached["status"] == "resolved" and cached["field_log_score"] is not None
    ):
        return False
    fetched = _parse_time(cached["fetched_at"])
    if now - fetched >= REFETCH_AFTER:
        return True
    due = cached["scheduled_resolve_time"]
    return due is not None and fetched < _parse_time(due) <= now


async def _fetch_post(
    client: httpx.AsyncClient, post_id: str, token: str | None
) -> dict[str, Any] | None:
    """GET one post; `None` if it no longer exists (404/410)."""
    if token is None:
        raise RuntimeError("METACULUS_TOKEN is not set; cannot fetch resolutions")
    response = await client.get(
        f"{API_BASE}/posts/{post_id}/",
        headers={
            "Authorization": f"Token {token}",
            "User-Agent": USER_AGENT,
            "Accept-Language": "en",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code in (404, 410):
        return None
    response.raise_for_status()
    post: dict[str, Any] = orjson.loads(response.content)
    return post


async def _refresh_resolutions(
    ledger: Ledger,
    token: str | None,
    client: httpx.AsyncClient,
    now: datetime,
    skipped: Counter[str],
) -> int:
    """Fetch every forecasted question whose cache is missing or stale."""
    fetched = 0
    for question in ledger.forecasted_questions():
        question_id = question["question_id"]
        if not _needs_fetch(question, ledger.get_resolution(question_id), now):
            continue
        match = _POST_ID_RE.search(question["url"] or "")
        if match is None:
            skipped["no_post_id"] += 1
            continue
        if fetched:
            await asyncio.sleep(REQUEST_PAUSE_SECONDS)
        post = await _fetch_post(client, match.group(1), token)
        fetched += 1
        if post is None:
            skipped["post_not_found"] += 1
        row = parse_post(post or {}, question_id)
        ledger.upsert_resolution(question_id, fetched_at=_utc_iso(now), **row)
    return fetched


def _score_resolved_questions(ledger: Ledger, skipped: Counter[str]) -> Counter[str]:
    """Write or update a `model_scores` row for each (run, model) of a resolved
    question; returns how many rows changed, by method."""
    changed: Counter[str] = Counter()
    for question in ledger.forecasted_questions():
        cached = ledger.get_resolution(question["question_id"])
        if (
            cached is None
            or cached["status"] != "resolved"
            or not cached["scaling_json"]
        ):
            continue
        scaling: Scaling = orjson.loads(cached["scaling_json"])
        for row in ledger.last_ok_forecasts(question["question_id"]):
            try:
                result = relative_score(
                    orjson.loads(row["forecast_json"]),
                    cached["field_log_score"],
                    cached["resolution"],
                    scaling,
                )
            except ValueError:
                result = None
            if result is None:
                skipped["forecast_does_not_fit"] += 1
                continue
            score, method = result
            changed[method] += ledger.record_model_score(
                row["run_id"], row["model_id"], question["question_id"], score, method
            )
    return +changed


def compute_weights(
    scores: dict[str, list[float]], enabled: Sequence[str]
) -> tuple[dict[str, float], dict[str, int]]:
    """Turn per-model question scores into selection weights.

    A model with fewer than `MIN_SCORED_QUESTIONS` scores is neutral (s = 0).
    Otherwise s = mean * n / (n + SHRINKAGE_PRIOR). Raw weight is
    exp(s / TEMPERATURE), divided by the mean over `enabled` models so the
    average is 1.0, then floored at `WEIGHT_FLOOR` (which can lift the final
    average slightly above 1.0).

    Returns:
        `(weights, n_scored)`, both keyed by model id for every enabled model.
    """
    n_scored = {model: len(scores.get(model, [])) for model in enabled}
    shrunk = {
        model: (
            sum(scores[model]) / n * n / (n + SHRINKAGE_PRIOR)
            if n >= MIN_SCORED_QUESTIONS
            else 0.0
        )
        for model, n in n_scored.items()
    }
    raw = {model: math.exp(s / TEMPERATURE) for model, s in shrunk.items()}
    mean_raw = sum(raw.values()) / len(raw) if raw else 1.0
    return {m: max(r / mean_raw, WEIGHT_FLOOR) for m, r in raw.items()}, n_scored


def _scores_by_model(ledger: Ledger) -> dict[str, list[float]]:
    """One score per (model, question): the model's latest run on it."""
    latest: dict[tuple[str, str], tuple[int, float]] = {}
    for row in ledger.get_model_scores():
        key = (row["model_id"], row["question_id"])
        if key not in latest or row["run_id"] > latest[key][0]:
            latest[key] = (row["run_id"], row["score"])
    by_model: dict[str, list[float]] = {}
    for (model_id, _), (_, score) in latest.items():
        by_model.setdefault(model_id, []).append(score)
    return by_model


def weights_updated_today(data_dir: Path, now: datetime) -> bool:
    """True if `weights.json` records an update on `now`'s UTC date."""
    path = data_dir / "weights.json"
    if not path.exists():
        return False
    try:
        updated_at = orjson.loads(path.read_bytes()).get("updated_at")
    except orjson.JSONDecodeError:
        return False
    return (
        isinstance(updated_at, str)
        and updated_at[:10] == now.astimezone(UTC).date().isoformat()
    )


async def update_weights(
    ledger: Ledger,
    data_dir: Path,
    pool_path: Path,
    token: str | None,
    *,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> UpdateSummary:
    """Fetch new resolutions, score every model, and rewrite `weights.json`.

    Network failures other than a missing post propagate; fetched questions
    stay cached, so the next call resumes where this one stopped.

    Args:
        ledger: The ledger holding questions, forecasts, `resolutions` and
            `model_scores`.
        data_dir: Where `weights.json` is written.
        pool_path: The model pool YAML; weights cover its enabled models.
        token: Metaculus API token (never logged).
        client: HTTP client to use; one is created when omitted.
        now: The current time, for tests. Defaults to the real clock.

    Returns:
        Counts and the written weights.

    Raises:
        RuntimeError: If a fetch is needed and `token` is `None`.
        httpx.HTTPError: On a failed request other than a 404 or 410.
    """
    started_at = now or datetime.now(UTC)
    skipped: Counter[str] = Counter()
    if client is None:
        async with httpx.AsyncClient() as owned_client:
            fetched = await _refresh_resolutions(
                ledger, token, owned_client, started_at, skipped
            )
    else:
        fetched = await _refresh_resolutions(ledger, token, client, started_at, skipped)
    by_method = _score_resolved_questions(ledger, skipped)
    scored = sum(by_method.values())

    enabled = [model.id for model in load_pool(pool_path).enabled_models]
    weights, n_scored = compute_weights(_scores_by_model(ledger), enabled)
    # Stamped when written, not when started, so a run that crosses midnight UTC
    # does not look stale to the host's same-day check.
    write_weights(data_dir, weights, n_scored, _utc_iso(now or datetime.now(UTC)))

    summary = UpdateSummary(
        fetched, scored, dict(by_method), dict(skipped), weights, n_scored
    )
    logger.info(
        "weight update: fetched=%d scored=%d by_method=%s skipped=%s models=%d "
        "models_with_enough_scores=%d",
        fetched,
        scored,
        dict(by_method),
        dict(skipped),
        len(weights),
        sum(n >= MIN_SCORED_QUESTIONS for n in n_scored.values()),
    )
    return summary
