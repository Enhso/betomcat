"""Tests for the budget pacing guard (context filter, pacing bands, add-back,
fail-open, and ledger-based cost estimate)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from betomcat.budget import (
    KEY_STATUS_URL,
    MINIBENCH_URL,
    BudgetGuard,
    KeyStatus,
    KeyStatusCache,
    MinibenchRoundCache,
    PacingResult,
    RoundAllowance,
    compute_daily_budget,
    compute_pace,
    compute_round_pace,
    estimate_cost,
    fetch_key_status,
    fetch_minibench_start,
    filter_by_context,
    select_eligible,
)
from betomcat.config import DEFAULT_BUDGET_WINDOW_END
from betomcat.ledger import Ledger
from betomcat.pool import ModelSpec


def _model(
    model_id: str,
    tier: str = "frontier",
    max_tokens: int = 4000,
    context_tokens: int | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    key: str | None = None,
) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        tier=tier,
        enabled=True,
        max_tokens=max_tokens,
        context_tokens=context_tokens,
        price_in=price_in,
        price_out=price_out,
        key=key,  # type: ignore[arg-type]
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


def _record_ok_cost(ledger: Ledger, model_id: str, cost: float) -> None:
    ledger.upsert_question(f"metaculus:{model_id}", "Will X?", "binary", None, None)
    run = ledger.start_run(f"metaculus:{model_id}")
    ledger.record_model_forecast(
        run.id,
        model_id,
        attempt=1,
        started_at="2026-09-22T19:00:00Z",
        finished_at="2026-09-22T19:00:05Z",
        status="ok",
        forecast=0.5,
        rationale="r",
        cost_usd=cost,
        tokens_in=10,
        tokens_out=5,
        error=None,
    )


# -- filter_by_context --------------------------------------------------------


def test_filter_by_context_keeps_models_with_unknown_window() -> None:
    model = _model("m", context_tokens=None)

    assert filter_by_context([model], prompt_chars=1_000_000) == [model]


def test_filter_by_context_drops_model_that_cannot_fit() -> None:
    # prompt_chars=3500 -> 1000 estimated input tokens; + max_tokens 4000 = 5000.
    small = _model("small", context_tokens=4000, max_tokens=4000)
    big = _model("big", context_tokens=100_000, max_tokens=4000)

    kept = filter_by_context([small, big], prompt_chars=3500)

    assert kept == [big]


def test_filter_by_context_boundary_is_inclusive() -> None:
    # estimated total exactly equals context_tokens -> kept.
    model = _model("m", context_tokens=5000, max_tokens=4000)

    kept = filter_by_context([model], prompt_chars=3500)

    assert kept == [model]


# -- estimate_cost -------------------------------------------------------------


def test_estimate_cost_uses_ledger_median_when_history_exists(ledger: Ledger) -> None:
    for cost in [0.10, 0.50, 0.30, 0.20, 0.40]:
        _record_ok_cost(ledger, "model-a", cost)
    model = _model("model-a", price_in=999, price_out=999)

    assert estimate_cost(model, ledger) == pytest.approx(0.30)


def test_estimate_cost_falls_back_to_price_when_no_history(ledger: Ledger) -> None:
    model = _model("model-a", price_in=2.0, price_out=10.0)

    # 12k input + 10k output tokens, at $2/$10 per MTok.
    expected = (2.0 * 12_000 + 10.0 * 10_000) / 1_000_000
    assert estimate_cost(model, ledger) == pytest.approx(expected)


def test_estimate_cost_free_model_with_no_price_is_zero(ledger: Ledger) -> None:
    model = _model("model-free")

    assert estimate_cost(model, ledger) == 0.0


def test_estimate_cost_ignores_legacy_zero_rows_for_a_priced_model(
    ledger: Ledger,
) -> None:
    # Legacy rows recorded 0 for every BYOK call before the llm.py cost fix.
    for cost in [0.0, 0.0, 0.30, 0.0, 0.20]:
        _record_ok_cost(ledger, "model-a", cost)
    model = _model("model-a", price_in=999, price_out=999)

    assert estimate_cost(model, ledger) == pytest.approx(0.25)


def test_estimate_cost_falls_back_to_price_when_all_recent_costs_are_legacy_zero(
    ledger: Ledger,
) -> None:
    for cost in [0.0, 0.0, 0.0]:
        _record_ok_cost(ledger, "model-a", cost)
    model = _model("model-a", price_in=2.0, price_out=10.0)

    expected = (2.0 * 12_000 + 10.0 * 10_000) / 1_000_000
    assert estimate_cost(model, ledger) == pytest.approx(expected)


# -- compute_daily_budget / compute_pace ---------------------------------------


def test_compute_daily_budget_prorates_over_days_left() -> None:
    window_end = datetime(2026, 9, 25, tzinfo=UTC)
    now = datetime(2026, 9, 23, 6, 0, 0, tzinfo=UTC)

    # start_of_day_remaining = 10 + 2 = 12; days_left = 2 (9/23 -> 9/25).
    budget = compute_daily_budget(
        limit_remaining=10.0, usage_daily=2.0, window_end=window_end, now=now
    )

    assert budget == pytest.approx(6.0)


def test_compute_daily_budget_floors_days_left_at_one() -> None:
    window_end = datetime(2026, 9, 20, tzinfo=UTC)  # already past
    now = datetime(2026, 9, 23, tzinfo=UTC)

    budget = compute_daily_budget(
        limit_remaining=10.0, usage_daily=0.0, window_end=window_end, now=now
    )

    assert budget == pytest.approx(10.0)


def test_compute_pace_uses_elapsed_fraction_floor() -> None:
    now = datetime(2026, 9, 23, 0, 30, 0, tzinfo=UTC)  # ~2% of the day elapsed

    # allowed_so_far = daily_budget * max(0.02, 0.1) = daily_budget * 0.1
    pace = compute_pace(usage_daily=1.0, daily_budget=10.0, now=now)

    assert pace == pytest.approx(1.0 / (10.0 * 0.1))


def test_compute_pace_midday() -> None:
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)  # 50% elapsed

    pace = compute_pace(usage_daily=5.0, daily_budget=10.0, now=now)

    assert pace == pytest.approx(1.0)


# -- KeyStatus.today_spend (BYOK, C3c) -----------------------------------------


def test_today_spend_adds_byok_usage_when_included_in_limit() -> None:
    status = KeyStatus(
        usage_daily=0.0,
        byok_usage_daily=0.3468,
        include_byok_in_limit=True,
    )

    assert status.today_spend == pytest.approx(0.3468)


def test_today_spend_ignores_byok_usage_when_not_included_in_limit() -> None:
    status = KeyStatus(
        usage_daily=0.1,
        byok_usage_daily=0.3468,
        include_byok_in_limit=False,
    )

    assert status.today_spend == pytest.approx(0.1)


def test_today_spend_tolerates_missing_byok_fields() -> None:
    status = KeyStatus(usage_daily=0.1)

    assert status.today_spend == pytest.approx(0.1)


def test_today_spend_is_none_when_usage_daily_missing() -> None:
    status = KeyStatus(byok_usage_daily=0.5, include_byok_in_limit=True)

    assert status.today_spend is None


async def test_fetch_key_status_parses_byok_fields(httpx_mock: HTTPXMock) -> None:
    # Verified live 2026-09-23: the funded (BYOK) key's GET /api/v1/key.
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={
            "data": {
                "limit_remaining": 42.5,
                "usage_daily": 0.0,
                "byok_usage_daily": 0.3468,
                "include_byok_in_limit": True,
            }
        },
    )
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.ok is True
    assert status.byok_usage_daily == pytest.approx(0.3468)
    assert status.include_byok_in_limit is True
    assert status.today_spend == pytest.approx(0.3468)


async def test_fetch_key_status_tolerates_missing_byok_fields(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": 42.5, "usage_daily": 1.0}},
    )
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.ok is True
    assert status.byok_usage_daily is None
    assert status.include_byok_in_limit is None
    assert status.today_spend == pytest.approx(1.0)


def test_select_eligible_byok_usage_pushes_pace_into_harsher_band(
    ledger: Ledger,
) -> None:
    """A funded (BYOK) key with usage_daily=0 but real spend in
    byok_usage_daily must still drive pacing -- otherwise expensive models
    look free and are never dropped."""
    medium = _model("medium", price_in=2, price_out=10)  # est cost 0.124
    cheap1 = _model("cheap1")  # padding
    cheap2 = _model("cheap2")  # padding
    funded = KeyStatus(
        ok=True,
        limit_remaining=11.0,
        usage_daily=0.0,
        byok_usage_daily=1.0,
        include_byok_in_limit=True,
    )
    late = datetime(2026, 9, 23, 23, 45, 0, tzinfo=UTC)

    result = select_eligible(
        [medium, cheap1, cheap2], funded, KeyStatus(ok=False), ledger, WINDOW_END, late
    )

    assert result.pace is not None and result.pace > 1.0
    assert result.excluded_ids == ["medium"]


def test_select_eligible_byok_usage_ignored_when_not_included_in_limit(
    ledger: Ledger,
) -> None:
    medium = _model("medium", price_in=2, price_out=10)  # est cost 0.124
    cheap1 = _model("cheap1")
    cheap2 = _model("cheap2")
    funded = KeyStatus(
        ok=True,
        limit_remaining=11.0,
        usage_daily=0.0,
        byok_usage_daily=1.0,
        include_byok_in_limit=False,
    )

    result = select_eligible(
        [medium, cheap1, cheap2], funded, KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert result.pace == pytest.approx(0.0)
    assert result.excluded_ids == []


# -- select_eligible: pacing bands ---------------------------------------------

WINDOW_END = datetime(2026, 10, 5, tzinfo=UTC)
NOON = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)  # 50% of day elapsed


def _funded(limit_remaining: float, usage_daily: float) -> KeyStatus:
    return KeyStatus(ok=True, limit_remaining=limit_remaining, usage_daily=usage_daily)


def _usage_daily_for_pace(
    target_pace: float, limit_remaining: float, window_end: datetime, now: datetime
) -> float:
    """Binary-search `usage_daily` so `compute_pace(...)` lands on `target_pace`.

    Solved numerically (rather than by hand) against the real
    `compute_daily_budget`/`compute_pace` so these tests can't drift from the
    implementation's exact arithmetic.
    """
    lo, hi = 0.0, max(limit_remaining, 1.0) * 1000.0
    for _ in range(80):
        mid = (lo + hi) / 2
        budget = compute_daily_budget(limit_remaining, mid, window_end, now)
        pace = compute_pace(mid, budget, now)
        if pace < target_pace:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def test_select_eligible_pace_at_or_below_one_excludes_nothing(ledger: Ledger) -> None:
    cheap = _model("cheap", price_in=0.1, price_out=0.5)
    expensive = _model("expensive", price_in=10, price_out=50)
    usage_daily = _usage_daily_for_pace(1.0, 88.0, WINDOW_END, NOON)
    funded = _funded(limit_remaining=88.0, usage_daily=usage_daily)

    result = select_eligible(
        [cheap, expensive], funded, KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert result.pace == pytest.approx(1.0, abs=1e-3)
    assert result.excluded_ids == []
    assert {m.id for m in result.eligible} == {"cheap", "expensive"}


def test_select_eligible_band_1_to_1_5_drops_ge_040(ledger: Ledger) -> None:
    cheap = _model("cheap", price_in=0.1, price_out=0.5)  # est cost < 0.40
    padding = _model("padding", price_in=0.1, price_out=0.5)  # keeps pool >= 2
    pricey = _model("pricey", price_in=10, price_out=50)  # est cost >= 0.40
    usage_daily = _usage_daily_for_pace(1.3, 88.0, WINDOW_END, NOON)
    funded = _funded(limit_remaining=88.0, usage_daily=usage_daily)

    result = select_eligible(
        [cheap, padding, pricey], funded, KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert 1.0 < result.pace < 1.5  # type: ignore[operator]
    assert result.excluded_ids == ["pricey"]


def test_select_eligible_band_1_5_to_2_drops_ge_020(ledger: Ledger) -> None:
    tiny = _model("tiny", price_in=0.1, price_out=0.5)  # est cost 0.0062, < 0.20
    padding = _model("padding", price_in=0.1, price_out=0.5)  # keeps pool >= 2
    mid = _model("mid", price_in=4, price_out=20)  # est cost 0.248, >= 0.20
    usage_daily = _usage_daily_for_pace(1.8, 88.0, WINDOW_END, NOON)
    funded = _funded(limit_remaining=88.0, usage_daily=usage_daily)

    result = select_eligible(
        [tiny, padding, mid], funded, KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert 1.5 < result.pace < 2.0  # type: ignore[operator]
    assert result.excluded_ids == ["mid"]
    assert "tiny" not in result.excluded_ids


def test_select_eligible_pace_over_2_drops_ge_005(ledger: Ledger) -> None:
    free_ish = _model("free-ish")  # no price -> est cost 0.0
    padding = _model("padding")  # keeps pool >= 2, also est cost 0.0
    small = _model("small", price_in=2, price_out=10)  # est cost 0.124, >= 0.05
    usage_daily = _usage_daily_for_pace(2.4, 88.0, WINDOW_END, NOON)
    funded = _funded(limit_remaining=88.0, usage_daily=usage_daily)

    result = select_eligible(
        [free_ish, padding, small],
        funded,
        KeyStatus(ok=False),
        ledger,
        WINDOW_END,
        NOON,
    )

    assert result.pace is not None and result.pace > 2.0
    assert result.excluded_ids == ["small"]


def test_select_eligible_usage_over_daily_budget_forces_005_band_even_at_low_pace(
    ledger: Ledger,
) -> None:
    medium = _model("medium", price_in=2, price_out=10)  # est cost 0.124
    cheap1 = _model("cheap1")  # padding, est cost 0.0
    cheap2 = _model("cheap2")  # padding, est cost 0.0
    # Late in the day (elapsed ~99%), pace alone lands in the mild 1-1.5 band
    # (a $0.40 bar); but usage_daily has already reached daily_budget, so the
    # "or usage_daily >= daily_budget" clause must still force the harshest
    # ($0.05) band, catching "medium" ($0.124) the milder band would spare.
    late = datetime(2026, 9, 23, 23, 45, 0, tzinfo=UTC)
    funded = _funded(limit_remaining=11.0, usage_daily=1.0)

    result = select_eligible(
        [medium, cheap1, cheap2], funded, KeyStatus(ok=False), ledger, WINDOW_END, late
    )

    assert result.pace is not None and 1.0 < result.pace < 1.5
    assert result.daily_budget is not None and result.daily_budget <= 1.0
    assert result.excluded_ids == ["medium"]


def test_select_eligible_low_limit_remaining_drops_paid_models_only(
    ledger: Ledger,
) -> None:
    paid = _model("paid", tier="frontier", price_in=1.0, price_out=1.0)
    paid2 = _model("paid2", tier="frontier", price_in=1.0, price_out=1.0)
    free = _model("free", tier="free", price_in=0.0, price_out=0.0)
    free2 = _model("free2", tier="free", price_in=0.0, price_out=0.0)
    # pace == 0 (usage_daily=0) so the cost-threshold bands don't fire --
    # only the limit_remaining < $3 rule is under test here.
    funded = _funded(limit_remaining=2.5, usage_daily=0.0)

    result = select_eligible(
        [paid, paid2, free, free2],
        funded,
        KeyStatus(ok=False),
        ledger,
        WINDOW_END,
        NOON,
    )

    assert result.pace == 0.0
    assert set(result.excluded_ids) == {"paid", "paid2"}
    assert "free" not in result.excluded_ids
    assert "free2" not in result.excluded_ids


def test_select_eligible_free_key_low_quota_drops_free_key_models(
    ledger: Ledger,
) -> None:
    free_key_model = _model("free-key-model", key="free")
    free_key_model2 = _model("free-key-model2", key="free")
    funded_key_free_model = _model("funded-free-model", key="funded")
    funded_key_free_model2 = _model("funded-free-model2", key="funded")
    free_status = KeyStatus(ok=True, free_daily_requests_remaining=1)

    result = select_eligible(
        [
            free_key_model,
            free_key_model2,
            funded_key_free_model,
            funded_key_free_model2,
        ],
        KeyStatus(ok=False),
        free_status,
        ledger,
        WINDOW_END,
        NOON,
    )

    assert set(result.excluded_ids) == {"free-key-model", "free-key-model2"}
    assert "funded-free-model" not in result.excluded_ids
    assert "funded-free-model2" not in result.excluded_ids


# -- select_eligible: add-back rule --------------------------------------------


def test_select_eligible_adds_back_cheapest_when_fewer_than_two_remain(
    ledger: Ledger,
) -> None:
    a = _model("a", price_in=10, price_out=50)  # expensive
    b = _model("b", price_in=8, price_out=40)  # cheaper of the excluded two
    c = _model("c", price_in=0.01, price_out=0.01)  # survives on its own
    funded = _funded(
        limit_remaining=88.0, usage_daily=10.0
    )  # pace > 2 -> >=0.05 dropped

    result = select_eligible(
        [a, b, c], funded, KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert len(result.eligible) >= 2
    eligible_ids = {m.id for m in result.eligible}
    assert "c" in eligible_ids
    # "b" is cheaper than "a" among the excluded, so it's the one added back.
    assert "b" in eligible_ids
    assert "b" not in result.excluded_ids


def test_select_eligible_add_back_no_op_when_nothing_excluded(ledger: Ledger) -> None:
    a = _model("a")
    b = _model("b")

    result = select_eligible(
        [a, b], KeyStatus(ok=False), KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert result.excluded_ids == []
    assert {m.id for m in result.eligible} == {"a", "b"}


# -- fail-open ------------------------------------------------------------------


def test_select_eligible_fails_open_when_funded_status_not_ok(ledger: Ledger) -> None:
    expensive = _model("expensive", price_in=999, price_out=999)

    result = select_eligible(
        [expensive], KeyStatus(ok=False), KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert result.pace is None
    assert result.daily_budget is None
    assert result.excluded_ids == []


def test_select_eligible_fails_open_when_fields_missing(ledger: Ledger) -> None:
    expensive = _model("expensive", price_in=999, price_out=999)
    # ok=True but the fields pacing needs are absent -- still fail open.
    status = KeyStatus(ok=True, limit_remaining=None, usage_daily=None)

    result = select_eligible(
        [expensive], status, KeyStatus(ok=False), ledger, WINDOW_END, NOON
    )

    assert result.pace is None
    assert result.excluded_ids == []


# -- fetch_key_status -----------------------------------------------------------


async def test_fetch_key_status_parses_nested_data_envelope(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": 42.5, "usage_daily": 3.25}},
    )
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.ok is True
    assert status.limit_remaining == pytest.approx(42.5)
    assert status.usage_daily == pytest.approx(3.25)


async def test_fetch_key_status_parses_free_daily_requests(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"free_model_daily_requests": {"remaining": 7}},
    )
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.free_daily_requests_remaining == 7


async def test_fetch_key_status_fails_open_on_http_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=KEY_STATUS_URL, status_code=500, text="server error")
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.ok is False


async def test_fetch_key_status_fails_open_on_malformed_json(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL, content=b"not json", headers={"content-type": "text/plain"}
    )
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.ok is False


async def test_fetch_key_status_fails_open_on_malformed_field_types(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL, json={"data": {"limit_remaining": "not-a-number"}}
    )
    async with httpx.AsyncClient() as client:
        status = await fetch_key_status(client, "some-key")

    assert status.ok is False


async def test_fetch_key_status_never_logs_the_key(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    httpx_mock.add_response(url=KEY_STATUS_URL, status_code=401, text="unauthorized")
    caplog.set_level("WARNING")
    async with httpx.AsyncClient() as client:
        await fetch_key_status(client, "super-secret-key")

    assert "super-secret-key" not in caplog.text


# -- KeyStatusCache ---------------------------------------------------------------


async def test_key_status_cache_reuses_within_ttl(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL, json={"data": {"limit_remaining": 1.0, "usage_daily": 0.0}}
    )
    clock_value = [0.0]
    cache = KeyStatusCache(ttl_seconds=60.0, clock=lambda: clock_value[0])

    async with httpx.AsyncClient() as client:
        first = await cache.get(client, "key")
        clock_value[0] = 30.0  # still within the 60s TTL
        second = await cache.get(client, "key")

    assert first == second
    assert len(httpx_mock.get_requests()) == 1


async def test_key_status_cache_refetches_after_ttl(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL, json={"data": {"limit_remaining": 1.0, "usage_daily": 0.0}}
    )
    httpx_mock.add_response(
        url=KEY_STATUS_URL, json={"data": {"limit_remaining": 2.0, "usage_daily": 1.0}}
    )
    clock_value = [0.0]
    cache = KeyStatusCache(ttl_seconds=60.0, clock=lambda: clock_value[0])

    async with httpx.AsyncClient() as client:
        first = await cache.get(client, "key")
        clock_value[0] = 61.0  # past the TTL
        second = await cache.get(client, "key")

    assert first.limit_remaining == 1.0
    assert second.limit_remaining == 2.0
    assert len(httpx_mock.get_requests()) == 2


# -- BudgetGuard integration --------------------------------------------------


async def test_budget_guard_restrict_with_no_keys_configured_is_a_no_op(
    ledger: Ledger,
) -> None:
    guard = BudgetGuard()  # no funded/free key -> fail open, no network calls
    a = _model("a")
    b = _model("b")

    result = await guard.restrict([a, b], prompt_chars=100, ledger=ledger, now=NOON)

    assert result.pace is None
    assert {m.id for m in result.eligible} == {"a", "b"}


async def test_budget_guard_restrict_combines_context_filter_and_pacing(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": 88.0, "usage_daily": 10.0}},
    )
    too_small = _model("too-small", context_tokens=100, max_tokens=4000)
    expensive = _model("expensive", price_in=10, price_out=50, context_tokens=None)
    cheap = _model("cheap", price_in=0.01, price_out=0.01, context_tokens=None)
    cheap2 = _model("cheap2", price_in=0.01, price_out=0.01, context_tokens=None)

    async with httpx.AsyncClient() as http_client:
        guard = BudgetGuard(funded_api_key="funded-key", http_client=http_client)
        result = await guard.restrict(
            [too_small, expensive, cheap, cheap2],
            prompt_chars=350_000,  # ~100k estimated input tokens
            ledger=ledger,
            now=NOON,
        )

    eligible_ids = {m.id for m in result.eligible}
    assert "too-small" not in eligible_ids  # dropped by the context filter
    assert "expensive" not in eligible_ids  # dropped by pacing (pace > 2)
    assert "cheap" in eligible_ids


# -- MiniBench round mode ---------------------------------------------------------

ROUND_START = datetime(2026, 9, 21, tzinfo=UTC)
ROUND_START_TEXT = "2026-09-21T00:00:00Z"
IN_ROUND = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def _minibench_json(start_date: str = ROUND_START_TEXT) -> dict[str, object]:
    return {"id": 33125, "slug": "minibench", "start_date": start_date}


def test_compute_round_pace_is_spend_over_budget() -> None:
    assert compute_round_pace(20.0, 50.0) == pytest.approx(0.4)
    assert compute_round_pace(75.0, 50.0) == pytest.approx(1.5)


def test_compute_round_pace_with_no_budget() -> None:
    assert compute_round_pace(0.0, 0.0) == 0.0
    assert compute_round_pace(1.0, 0.0) == float("inf")


async def test_fetch_minibench_start_parses_start_date_and_sends_headers(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())

    async with httpx.AsyncClient() as client:
        start = await fetch_minibench_start(client, "meta-token")

    assert start == ROUND_START
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Token meta-token"
    assert request.headers["User-Agent"].startswith("Mozilla/5.0")


@pytest.mark.parametrize(
    "response",
    [
        {"status_code": 403, "text": "cloudflare"},
        {"status_code": 200, "text": "not json"},
        {"status_code": 200, "json": {"id": 1}},
        {"status_code": 200, "json": {"start_date": None}},
        {"status_code": 200, "json": {"start_date": "soon"}},
        {"status_code": 200, "json": ["start_date"]},
    ],
)
async def test_fetch_minibench_start_fails_open(
    httpx_mock: HTTPXMock, response: dict[str, object]
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, **response)  # type: ignore[arg-type]

    async with httpx.AsyncClient() as client:
        assert await fetch_minibench_start(client, "meta-token") is None


async def test_fetch_minibench_start_never_logs_the_token(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, status_code=403, text="blocked")
    caplog.set_level("WARNING")

    async with httpx.AsyncClient() as client:
        await fetch_minibench_start(client, "super-secret-token")

    assert "MiniBench round lookup failed" in caplog.text
    assert "super-secret-token" not in caplog.text


async def test_minibench_round_cache_reuses_within_ttl(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    clock_value = [0.0]
    cache = MinibenchRoundCache(ttl_seconds=300.0, clock=lambda: clock_value[0])

    async with httpx.AsyncClient() as client:
        first = await cache.get(client, "t")
        clock_value[0] = 299.0
        second = await cache.get(client, "t")

    assert first == second == ROUND_START
    assert len(httpx_mock.get_requests()) == 1


async def test_minibench_round_cache_refetches_after_ttl(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    httpx_mock.add_response(
        url=MINIBENCH_URL, json=_minibench_json("2026-10-05T00:00:00Z")
    )
    clock_value = [0.0]
    cache = MinibenchRoundCache(ttl_seconds=300.0, clock=lambda: clock_value[0])

    async with httpx.AsyncClient() as client:
        first = await cache.get(client, "t")
        clock_value[0] = 301.0
        second = await cache.get(client, "t")

    assert first == ROUND_START
    assert second == datetime(2026, 10, 5, tzinfo=UTC)
    assert len(httpx_mock.get_requests()) == 2


async def test_minibench_round_cache_caches_a_failed_lookup(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, status_code=403)
    clock_value = [0.0]
    cache = MinibenchRoundCache(ttl_seconds=300.0, clock=lambda: clock_value[0])

    async with httpx.AsyncClient() as client:
        first = await cache.get(client, "t")
        second = await cache.get(client, "t")

    assert first is None and second is None
    assert len(httpx_mock.get_requests()) == 1


def test_select_eligible_round_mode_excludes_nothing_under_budget(
    ledger: Ledger,
) -> None:
    cheap = _model("cheap", price_in=0.1, price_out=0.5)
    expensive = _model("expensive", price_in=10, price_out=50)
    # usage_daily this high would put the daily rule deep into the 0.05 band.
    funded = _funded(limit_remaining=60.0, usage_daily=40.0)

    result = select_eligible(
        [cheap, expensive],
        funded,
        KeyStatus(ok=False),
        ledger,
        WINDOW_END,
        IN_ROUND,
        round_allowance=RoundAllowance(spend_usd=30.0, budget_usd=50.0),
    )

    assert result.pace == pytest.approx(0.6)
    assert result.daily_budget == pytest.approx(50.0)
    assert result.excluded_ids == []


def test_select_eligible_round_mode_applies_the_ladder_over_budget(
    ledger: Ledger,
) -> None:
    cheap1 = _model("cheap1")
    cheap2 = _model("cheap2")
    medium = _model("medium", price_in=2, price_out=10)  # est cost 0.124
    expensive = _model("expensive", price_in=10, price_out=50)  # est cost 0.62
    funded = _funded(limit_remaining=30.0, usage_daily=0.0)

    result = select_eligible(
        [cheap1, cheap2, medium, expensive],
        funded,
        KeyStatus(ok=False),
        ledger,
        WINDOW_END,
        IN_ROUND,
        round_allowance=RoundAllowance(spend_usd=55.0, budget_usd=50.0),
    )

    assert result.pace == pytest.approx(1.1)
    # Spend >= budget is the ladder's 0.05 band, so it applies as soon as
    # pace passes 1 and the 0.40 / 0.20 bands never engage in round mode.
    assert result.excluded_ids == ["expensive", "medium"]


def test_select_eligible_round_mode_keeps_the_low_credit_rule(ledger: Ledger) -> None:
    cheap1 = _model("cheap1")
    cheap2 = _model("cheap2")
    medium = _model("medium", price_in=2, price_out=10)
    funded = _funded(limit_remaining=2.0, usage_daily=0.0)

    result = select_eligible(
        [cheap1, cheap2, medium],
        funded,
        KeyStatus(ok=False),
        ledger,
        WINDOW_END,
        IN_ROUND,
        round_allowance=RoundAllowance(spend_usd=1.0, budget_usd=50.0),
    )

    assert result.excluded_ids == ["medium"]


def test_select_eligible_round_mode_keeps_the_free_key_rule(ledger: Ledger) -> None:
    free_model = _model("free-a", tier="free", key="free")
    paid1 = _model("paid1")
    paid2 = _model("paid2")
    funded = _funded(limit_remaining=80.0, usage_daily=0.0)
    free_status = KeyStatus(ok=True, free_daily_requests_remaining=2)

    result = select_eligible(
        [free_model, paid1, paid2],
        funded,
        free_status,
        ledger,
        WINDOW_END,
        IN_ROUND,
        round_allowance=RoundAllowance(spend_usd=1.0, budget_usd=50.0),
    )

    assert result.excluded_ids == ["free-a"]


async def _restrict_with_mocks(
    httpx_mock: HTTPXMock,
    ledger: Ledger,
    now: datetime,
    limit_remaining: float = 88.0,
    usage_daily: float = 0.0,
    token: str | None = "meta-token",
    models: list[ModelSpec] | None = None,
) -> PacingResult:
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": limit_remaining, "usage_daily": usage_daily}},
    )
    pool = models or [
        _model("cheap1"),
        _model("cheap2"),
        _model("expensive", price_in=10, price_out=50),
    ]
    async with httpx.AsyncClient() as http_client:
        guard = BudgetGuard(
            funded_api_key="funded-key",
            http_client=http_client,
            metaculus_token=token,
        )
        return await guard.restrict(pool, prompt_chars=100, ledger=ledger, now=now)


async def test_guard_uses_round_mode_inside_the_busy_window(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    # The daily rule would put this in the 0.05 band (usage_daily 10 of ~$8).
    result = await _restrict_with_mocks(
        httpx_mock, ledger, IN_ROUND, limit_remaining=88.0, usage_daily=10.0
    )

    assert result.daily_budget == pytest.approx(50.0)
    assert result.pace == pytest.approx(0.0)
    assert result.excluded_ids == []


async def test_guard_round_window_start_is_inclusive_and_end_exclusive(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    async with httpx.AsyncClient() as http_client:
        guard = BudgetGuard(
            funded_api_key="funded-key",
            http_client=http_client,
            metaculus_token="meta-token",
            cache=KeyStatusCache(ttl_seconds=0.0),
        )
        httpx_mock.add_response(
            url=KEY_STATUS_URL,
            json={"data": {"limit_remaining": 88.0, "usage_daily": 0.0}},
            is_reusable=True,
        )
        pool = [_model("a"), _model("b")]
        outcomes = {}
        for label, now in {
            "before": ROUND_START - timedelta(seconds=1),
            "at_start": ROUND_START,
            "last_second": ROUND_START + timedelta(days=4) - timedelta(seconds=1),
            "at_end": ROUND_START + timedelta(days=4),
        }.items():
            result = await guard.restrict(pool, 100, ledger, now)
            outcomes[label] = result.daily_budget == pytest.approx(50.0)

    assert outcomes == {
        "before": False,
        "at_start": True,
        "last_second": True,
        "at_end": False,
    }


async def test_guard_uses_daily_rule_outside_the_busy_window(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    after_window = ROUND_START + timedelta(days=6, hours=12)

    result = await _restrict_with_mocks(httpx_mock, ledger, after_window)

    expected = compute_daily_budget(88.0, 0.0, DEFAULT_BUDGET_WINDOW_END, after_window)
    assert result.daily_budget == pytest.approx(expected)
    assert ledger.get_round_start(ROUND_START_TEXT) is None


async def test_guard_falls_back_to_daily_rule_when_the_lookup_fails(
    httpx_mock: HTTPXMock, ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, status_code=403, text="cloudflare")
    caplog.set_level("WARNING")

    result = await _restrict_with_mocks(httpx_mock, ledger, IN_ROUND)

    expected = compute_daily_budget(88.0, 0.0, DEFAULT_BUDGET_WINDOW_END, IN_ROUND)
    assert result.daily_budget == pytest.approx(expected)
    assert "MiniBench round lookup failed" in caplog.text
    assert "meta-token" not in caplog.text


async def test_guard_without_a_metaculus_token_uses_daily_rule_without_a_lookup(
    httpx_mock: HTTPXMock, ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")

    result = await _restrict_with_mocks(httpx_mock, ledger, IN_ROUND, token=None)

    expected = compute_daily_budget(88.0, 0.0, DEFAULT_BUDGET_WINDOW_END, IN_ROUND)
    assert result.daily_budget == pytest.approx(expected)
    assert [r.url.host for r in httpx_mock.get_requests()] == ["openrouter.ai"]
    assert "no Metaculus token" in caplog.text


async def test_guard_skips_the_round_lookup_when_the_funded_key_is_unreadable(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=KEY_STATUS_URL, status_code=500)
    async with httpx.AsyncClient() as http_client:
        guard = BudgetGuard(
            funded_api_key="funded-key",
            http_client=http_client,
            metaculus_token="meta-token",
        )
        result = await guard.restrict([_model("a"), _model("b")], 100, ledger, IN_ROUND)

    assert result.pace is None
    assert [r.url.host for r in httpx_mock.get_requests()] == ["openrouter.ai"]


async def test_guard_caches_the_round_lookup_across_questions(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": 88.0, "usage_daily": 0.0}},
        is_reusable=True,
    )
    async with httpx.AsyncClient() as http_client:
        guard = BudgetGuard(
            funded_api_key="funded-key",
            http_client=http_client,
            metaculus_token="meta-token",
            cache=KeyStatusCache(ttl_seconds=0.0),
        )
        for _ in range(3):
            await guard.restrict([_model("a"), _model("b")], 100, ledger, IN_ROUND)

    minibench_requests = [
        r for r in httpx_mock.get_requests() if r.url.host == "www.metaculus.com"
    ]
    assert len(minibench_requests) == 1


async def test_guard_records_the_round_start_value_on_first_sight_only(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": 95.0, "usage_daily": 0.0}},
    )
    httpx_mock.add_response(
        url=KEY_STATUS_URL,
        json={"data": {"limit_remaining": 80.0, "usage_daily": 0.0}},
    )
    async with httpx.AsyncClient() as http_client:
        guard = BudgetGuard(
            funded_api_key="funded-key",
            http_client=http_client,
            metaculus_token="meta-token",
            cache=KeyStatusCache(ttl_seconds=0.0),
        )
        first = await guard.restrict([_model("a"), _model("b")], 100, ledger, IN_ROUND)
        second = await guard.restrict([_model("a"), _model("b")], 100, ledger, IN_ROUND)

    assert ledger.get_round_start(ROUND_START_TEXT) == pytest.approx(95.0)
    assert first.pace == pytest.approx(0.0)
    assert second.pace == pytest.approx(15.0 / 50.0)


async def test_guard_round_spend_is_recorded_start_minus_current_remaining(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    ledger.record_round_start(ROUND_START_TEXT, 90.0)
    expensive = _model("expensive", price_in=10, price_out=50)

    result = await _restrict_with_mocks(
        httpx_mock,
        ledger,
        IN_ROUND,
        limit_remaining=45.0,
        models=[_model("cheap1"), _model("cheap2"), expensive],
    )

    assert result.pace == pytest.approx(45.0 / 50.0)
    assert result.excluded_ids == []


async def test_guard_round_over_budget_excludes_expensive_models(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    ledger.record_round_start(ROUND_START_TEXT, 99.0)
    expensive = _model("expensive", price_in=10, price_out=50)

    result = await _restrict_with_mocks(
        httpx_mock,
        ledger,
        IN_ROUND,
        limit_remaining=40.0,
        models=[_model("cheap1"), _model("cheap2"), expensive],
    )

    assert result.pace == pytest.approx(59.0 / 50.0)
    assert result.excluded_ids == ["expensive"]


async def test_guard_round_spend_never_goes_negative_after_a_credit_top_up(
    httpx_mock: HTTPXMock, ledger: Ledger
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    ledger.record_round_start(ROUND_START_TEXT, 40.0)

    result = await _restrict_with_mocks(
        httpx_mock, ledger, IN_ROUND, limit_remaining=99.0
    )

    assert result.pace == pytest.approx(0.0)


async def test_guard_logs_round_mode(
    httpx_mock: HTTPXMock, ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    httpx_mock.add_response(url=MINIBENCH_URL, json=_minibench_json())
    caplog.set_level("INFO", logger="betomcat.budget")

    await _restrict_with_mocks(httpx_mock, ledger, IN_ROUND)

    assert "MiniBench round mode" in caplog.text


# -- direct Google key: never paced out ---------------------------------------


def _google_model(context_tokens: int | None = None) -> ModelSpec:
    # Priced like a top-tier funded model so every band would catch it if it
    # were treated as one.
    return _model(
        "google/gemini-3.8-flash",
        price_in=10,
        price_out=50,
        key="google",
        context_tokens=context_tokens,
    )


def test_estimate_cost_is_zero_for_a_google_key_model_despite_price_and_history(
    ledger: Ledger,
) -> None:
    model = _google_model()
    for cost in (0.5, 0.7, 0.9):
        _record_ok_cost(ledger, model.id, cost)

    assert estimate_cost(model, ledger) == 0.0


def _daily_pace(target_pace: float) -> KeyStatus:
    usage_daily = _usage_daily_for_pace(target_pace, 88.0, WINDOW_END, NOON)
    return _funded(limit_remaining=88.0, usage_daily=usage_daily)


@pytest.mark.parametrize(
    ("funded", "round_allowance", "control"),
    [
        pytest.param(
            _daily_pace(1.3),
            None,
            _model("control", price_in=10, price_out=50),
            id="daily-band-0.40",
        ),
        pytest.param(
            _daily_pace(1.8),
            None,
            _model("control", price_in=10, price_out=50),
            id="daily-band-0.20",
        ),
        pytest.param(
            _daily_pace(2.4),
            None,
            _model("control", price_in=10, price_out=50),
            id="daily-band-0.05",
        ),
        pytest.param(
            _funded(limit_remaining=30.0, usage_daily=0.0),
            RoundAllowance(spend_usd=55.0, budget_usd=50.0),
            _model("control", price_in=10, price_out=50),
            id="round-mode-over-budget",
        ),
        pytest.param(
            _funded(limit_remaining=2.5, usage_daily=0.0),
            None,
            _model("control", price_in=1.0, price_out=1.0),
            id="funded-low-credit",
        ),
    ],
)
def test_select_eligible_never_excludes_a_google_key_model_by_funded_key_rules(
    ledger: Ledger,
    funded: KeyStatus,
    round_allowance: RoundAllowance | None,
    control: ModelSpec,
) -> None:
    google = _google_model()
    _record_ok_cost(ledger, google.id, 5.0)
    padding = _model("padding")

    result = select_eligible(
        [google, padding, control],
        funded,
        KeyStatus(ok=False),
        ledger,
        WINDOW_END,
        NOON,
        round_allowance=round_allowance,
    )

    assert result.excluded_ids == ["control"]
    assert google.id in {m.id for m in result.eligible}


def test_select_eligible_never_excludes_a_google_key_model_by_the_free_key_rule(
    ledger: Ledger,
) -> None:
    google = _google_model()
    padding = _model("padding")
    free_control = _model("free-control", tier="free", key="free")

    result = select_eligible(
        [google, padding, free_control],
        KeyStatus(ok=False),
        KeyStatus(ok=True, free_daily_requests_remaining=1),
        ledger,
        WINDOW_END,
        NOON,
    )

    assert result.excluded_ids == ["free-control"]
    assert google.id in {m.id for m in result.eligible}


async def test_guard_still_applies_the_context_filter_to_google_key_models(
    ledger: Ledger,
) -> None:
    too_small = _google_model(context_tokens=100)
    fits = _model("fits", key="google", context_tokens=None)

    result = await BudgetGuard().restrict(
        [too_small, fits], prompt_chars=350_000, ledger=ledger, now=NOON
    )

    assert [m.id for m in result.eligible] == ["fits"]
