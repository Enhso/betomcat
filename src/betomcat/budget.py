"""Budget pacing guard (spec s5; BUILD_LOG 2026-09-22 evening answers).

Restricts the pool handed to the spec s5 draw *before* the draw runs --
`pool.draw` itself is untouched; this module only decides which models are
still eligible by the time `draw` sees them. Two independent filters run in
order:

1. Context fit: drop any model whose context window can't hold the rendered
   prompt (`filter_by_context`).
2. Budget pacing: drop models progressively more aggressively as today's
   OpenRouter spend outruns a pro-rated daily budget, or as either key's
   remaining quota runs low (`select_eligible`).

Every external read (the OpenRouter key-status endpoint) fails open: a
network error, a non-2xx response, or a malformed body degrades to "unknown
status", which excludes nothing. This guard only ever narrows the pool, and
a broken status read must never itself cause a miss.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from betomcat.ledger import Ledger
from betomcat.pool import ModelSpec, effective_key

logger = logging.getLogger(__name__)

KEY_STATUS_URL = "https://openrouter.ai/api/v1/key"
KEY_STATUS_TIMEOUT = 10.0
CACHE_TTL_SECONDS = 60.0
CHARS_PER_TOKEN = 3.5
DEFAULT_INPUT_TOKENS = 12_000
DEFAULT_OUTPUT_TOKENS = 10_000
DEFAULT_WINDOW_END = datetime(2026, 10, 5, tzinfo=UTC)


def filter_by_context(
    models: Sequence[ModelSpec], prompt_chars: int
) -> list[ModelSpec]:
    """Drop models whose context window can't fit the rendered prompt.

    Estimated size = input tokens (`prompt_chars / 3.5`) plus the model's own
    reply budget (`max_tokens`). Models with no configured `context_tokens`
    are kept -- there is nothing to check them against.
    """
    estimated_input = prompt_chars / CHARS_PER_TOKEN
    kept = []
    for model in models:
        if model.context_tokens is None:
            kept.append(model)
            continue
        estimated_total = estimated_input + (model.max_tokens or 0)
        if estimated_total <= model.context_tokens:
            kept.append(model)
    return kept


def estimate_cost(model: ModelSpec, ledger: Ledger) -> float:
    """This model's estimated USD cost per call.

    Prefers the median of its last 10 successful ledger calls (empirical);
    falls back to a price-based estimate at 12k input + 10k output tokens
    (spec s5's own working estimate) when no history exists yet.

    For a model with a non-zero price, a recorded cost of exactly 0 is
    ignored: it's a legacy row from before the BYOK cost fix (llm.py) rather
    than a genuinely free call. A model with no configured price (a real
    free model) keeps its zero-cost history as-is.
    """
    recent = ledger.get_recent_costs(model.id, limit=10)
    price_in = model.price_in or 0.0
    price_out = model.price_out or 0.0
    if price_in or price_out:
        recent = [cost for cost in recent if cost != 0.0]
    if recent:
        return statistics.median(recent)
    return (
        price_in * DEFAULT_INPUT_TOKENS + price_out * DEFAULT_OUTPUT_TOKENS
    ) / 1_000_000


@dataclass(frozen=True)
class KeyStatus:
    """One OpenRouter key's budget status, or a failed/unknown read.

    `ok=False` means the status could not be read (network error, non-2xx,
    or a malformed body) -- callers must fail open and skip any exclusion
    gated on the missing field(s).

    `usage_daily` is 0 for a BYOK key (the funded key): its real day spend
    is in `byok_usage_daily`, only counted when `include_byok_in_limit` is
    true (`limit_remaining` is then already net of it -- verified live
    2026-09-23). `today_spend` combines the two for every caller that needs
    "how much has this key spent today".
    """

    ok: bool = True
    limit_remaining: float | None = None
    usage_daily: float | None = None
    free_daily_requests_remaining: int | None = None
    byok_usage_daily: float | None = None
    include_byok_in_limit: bool | None = None

    @property
    def today_spend(self) -> float | None:
        """`usage_daily` plus BYOK spend, when that spend counts toward the limit."""
        if self.usage_daily is None:
            return None
        if self.include_byok_in_limit and self.byok_usage_daily is not None:
            return self.usage_daily + self.byok_usage_daily
        return self.usage_daily


async def fetch_key_status(client: httpx.AsyncClient, api_key: str) -> KeyStatus:
    """`GET /api/v1/key`, parsed into a `KeyStatus`. Never raises; fails open."""
    try:
        response = await client.get(
            KEY_STATUS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=KEY_STATUS_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("budget: key status request failed (%s)", type(exc).__name__)
        return KeyStatus(ok=False)

    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    try:
        limit_remaining = data.get("limit_remaining")
        usage_daily = data.get("usage_daily")
        byok_usage_daily = data.get("byok_usage_daily")
        include_byok_in_limit = data.get("include_byok_in_limit")
        remaining = (data.get("free_model_daily_requests") or {}).get("remaining")
        return KeyStatus(
            limit_remaining=None if limit_remaining is None else float(limit_remaining),
            usage_daily=None if usage_daily is None else float(usage_daily),
            free_daily_requests_remaining=(
                None if remaining is None else int(remaining)
            ),
            byok_usage_daily=(
                None if byok_usage_daily is None else float(byok_usage_daily)
            ),
            include_byok_in_limit=(
                None if include_byok_in_limit is None else bool(include_byok_in_limit)
            ),
        )
    except (TypeError, ValueError, AttributeError) as exc:
        logger.warning("budget: key status payload malformed (%s)", type(exc).__name__)
        return KeyStatus(ok=False)


class KeyStatusCache:
    """60s TTL cache over `fetch_key_status`, keyed by the API key used."""

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, KeyStatus]] = {}

    async def get(self, client: httpx.AsyncClient, api_key: str) -> KeyStatus:
        now = self._clock()
        cached = self._entries.get(api_key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        status = await fetch_key_status(client, api_key)
        self._entries[api_key] = (now, status)
        return status


def compute_daily_budget(
    limit_remaining: float,
    usage_daily: float,
    window_end: datetime,
    now: datetime,
) -> float:
    """Pro-rated daily USD budget for the funded key, recomputed each UTC day."""
    now = now.astimezone(UTC)
    start_of_day_remaining = limit_remaining + usage_daily
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_left = max((window_end - start_of_today).total_seconds() / 86400.0, 1.0)
    return start_of_day_remaining / days_left


def compute_pace(usage_daily: float, daily_budget: float, now: datetime) -> float:
    """How far today's spend is running ahead of the pro-rated allowance."""
    now = now.astimezone(UTC)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_fraction = (now - start_of_today).total_seconds() / 86400.0
    allowed_so_far = daily_budget * max(elapsed_fraction, 0.1)
    if allowed_so_far <= 0:
        return float("inf") if usage_daily > 0 else 0.0
    return usage_daily / allowed_so_far


def _cost_threshold(
    pace: float, usage_daily: float, daily_budget: float
) -> float | None:
    """The est-cost exclusion bar for the given pace, or `None` for no exclusion."""
    if pace > 2 or usage_daily >= daily_budget:
        return 0.05
    if pace > 1.5:
        return 0.20
    if pace > 1:
        return 0.40
    return None


@dataclass(frozen=True)
class PacingResult:
    """The eligible pool for this draw, plus the pacing state behind it."""

    eligible: list[ModelSpec]
    pace: float | None
    daily_budget: float | None
    excluded_ids: list[str]


def select_eligible(
    models: Sequence[ModelSpec],
    funded_status: KeyStatus,
    free_status: KeyStatus,
    ledger: Ledger,
    window_end: datetime,
    now: datetime,
) -> PacingResult:
    """Apply the budget-pacing exclusion bands to an already context-filtered pool.

    `models` is assumed already filtered by `filter_by_context`; this only
    adds pacing-driven exclusions on top, then applies the fewer-than-2
    add-back rule (cheapest excluded model first).
    """
    excluded: dict[str, str] = {}
    pace: float | None = None
    daily_budget: float | None = None

    if (
        funded_status.ok
        and funded_status.limit_remaining is not None
        and funded_status.usage_daily is not None
    ):
        limit_remaining = funded_status.limit_remaining
        usage_daily = funded_status.today_spend
        assert usage_daily is not None  # guarded by the `usage_daily is not None` check
        daily_budget = compute_daily_budget(
            limit_remaining, usage_daily, window_end, now
        )
        pace = compute_pace(usage_daily, daily_budget, now)

        threshold = _cost_threshold(pace, usage_daily, daily_budget)
        if threshold is not None:
            for model in models:
                if model.id in excluded:
                    continue
                if estimate_cost(model, ledger) >= threshold:
                    excluded[model.id] = (
                        f"pace {pace:.2f}, est cost >= ${threshold:.2f}"
                    )

        if limit_remaining < 3.0:
            for model in models:
                if model.id in excluded or model.tier == "free":
                    continue
                if estimate_cost(model, ledger) >= 0.01:
                    excluded[model.id] = "funded key limit_remaining < $3"

    if (
        free_status.ok
        and free_status.free_daily_requests_remaining is not None
        and free_status.free_daily_requests_remaining < 3
    ):
        for model in models:
            if model.id in excluded:
                continue
            if effective_key(model) == "free":
                excluded[model.id] = "free key daily requests remaining < 3"

    eligible = [m for m in models if m.id not in excluded]

    if len(eligible) < 2:
        add_back_order = sorted(
            (m for m in models if m.id in excluded),
            key=lambda m: estimate_cost(m, ledger),
        )
        for model in add_back_order:
            if len(eligible) >= 2:
                break
            eligible.append(model)
            del excluded[model.id]

    return PacingResult(
        eligible=eligible,
        pace=pace,
        daily_budget=daily_budget,
        excluded_ids=sorted(excluded),
    )


@dataclass
class BudgetGuard:
    """Restricts the model pool for budget pacing, before the spec s5 draw.

    Wraps live OpenRouter key status (cached 60s) plus this bot's own ledger
    cost history into one `restrict` call. A key left unset (or a failed
    status read) fails open for that key's exclusions -- this guard only
    ever narrows the pool, never blocks a run on its own account.
    """

    funded_api_key: str | None = None
    free_api_key: str | None = None
    window_end: datetime = field(default_factory=lambda: DEFAULT_WINDOW_END)
    http_client: httpx.AsyncClient | None = None
    cache: KeyStatusCache = field(default_factory=KeyStatusCache)

    async def _status(self, api_key: str | None) -> KeyStatus:
        if not api_key:
            return KeyStatus(ok=False)
        if self.http_client is not None:
            return await self.cache.get(self.http_client, api_key)
        async with httpx.AsyncClient() as client:
            return await self.cache.get(client, api_key)

    async def restrict(
        self,
        models: Sequence[ModelSpec],
        prompt_chars: int,
        ledger: Ledger,
        now: datetime,
    ) -> PacingResult:
        context_ok = filter_by_context(models, prompt_chars)
        funded_status = await self._status(self.funded_api_key)
        free_status = await self._status(self.free_api_key)
        return select_eligible(
            context_ok, funded_status, free_status, ledger, self.window_end, now
        )
