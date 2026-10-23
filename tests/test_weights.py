"""Tests for the daily weight update: scoring, peer score, caching, weights.

Fixtures in `tests/fixtures/metaculus_post_*.json` are real `GET /api/posts/<id>/`
responses (MiniBench 2026-10-05, project 33129; the date post is from another
tournament). The four resolved ones keep the bot's `my_forecasts.score_data` and
`my_forecasts.latest.forecast_values` and nothing else under `my_forecasts`;
author, vote and other user-specific fields and the long question texts were
stripped. The post loaders below drop `my_forecasts` unless asked to keep it, so
a test sees a post with no peer score (baseline scoring) by default.

Numbers marked "by hand" are worked out in the comment beside them, not read
back from the code under test.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat import weights as weights_module
from betomcat.ledger import Ledger
from betomcat.pool import load_weights
from betomcat.weights import (
    compute_weights,
    update_weights,
    weights_updated_today,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CLOSED = "2026-10-12T00:00:00+00:00"

POOL_YAML = """
ensemble_width: 2
models:
  - {id: model-a, tier: frontier, enabled: true}
  - {id: model-b, tier: frontier, enabled: true}
  - {id: model-c, tier: free, enabled: true}
  - {id: model-e, tier: free, enabled: true}
  - {id: model-off, tier: free, enabled: false}
"""


@pytest.fixture(autouse=True)
def no_pause_between_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weights_module, "REQUEST_PAUSE_SECONDS", 0.0)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


@pytest.fixture
def pool_path(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(POOL_YAML)
    return path


def load_post(name: str) -> dict[str, Any]:
    post: dict[str, Any] = orjson.loads((FIXTURES / name).read_bytes())
    return post


def fixture_post(name: str, bot_scores: bool) -> dict[str, Any]:
    post = load_post(name)
    if not bot_scores:
        post["question"].pop("my_forecasts", None)
    return post


def binary_post(bot_scores: bool = False) -> dict[str, Any]:
    """Resolves "no"; the bot gave P(no) 0.815."""
    return fixture_post("metaculus_post_45948_binary.json", bot_scores)


def mc_post(bot_scores: bool = False) -> dict[str, Any]:
    """Resolves "Medium"; the bot gave it 0.57."""
    return fixture_post("metaculus_post_45904_multiple_choice.json", bot_scores)


def numeric_post(bot_scores: bool = False) -> dict[str, Any]:
    """Resolves 2664.0, bin 28; the bot's CDF puts 0.0195218334 there."""
    return fixture_post("metaculus_post_45950_numeric.json", bot_scores)


def discrete_post(bot_scores: bool = False) -> dict[str, Any]:
    """Resolves 19.0, bin 3; the bot's CDF puts 0.038089824 there."""
    return fixture_post("metaculus_post_45892_discrete.json", bot_scores)


def date_post() -> dict[str, Any]:
    return load_post("metaculus_post_42863_date.json")


def with_resolution(post: dict[str, Any], resolution: str | None) -> dict[str, Any]:
    post = copy.deepcopy(post)
    post["question"]["resolution"] = resolution
    return post


def cdf_from_masses(lower_tail: float, masses: list[float]) -> list[float]:
    cdf = [lower_tail]
    for mass in masses:
        cdf.append(cdf[-1] + mass)
    return cdf


def numeric_cdf(bin_28_mass: float) -> list[float]:
    """201-point CDF: 0.005 in each tail, `bin_28_mass` in bin 28, rest even."""
    rest = (1 - 0.01 - bin_28_mass) / 199
    masses = [bin_28_mass if i == 28 else rest for i in range(200)]
    return cdf_from_masses(0.005, masses)


DISCRETE_CDF = [0.0, 0.1, 0.3, 0.6, 0.9, 0.97]  # masses .1 .2 .3 .3 .07, tail .03


def seed(
    ledger: Ledger,
    post: dict[str, Any],
    forecasts: dict[str, object],
    close_time: str = CLOSED,
) -> int:
    """Record a question and one run in which each model made an `ok` forecast."""
    question_id = f"metaculus:{post['question']['id']}"
    ledger.upsert_question(
        question_id,
        post["title"],
        post["question"]["type"],
        close_time,
        f"https://www.metaculus.com/questions/{post['id']}",
    )
    run = ledger.start_run(question_id)
    for model_id, forecast in forecasts.items():
        record_forecast(ledger, run.id, model_id, forecast)
    return run.id


def record_forecast(
    ledger: Ledger,
    run_id: int,
    model_id: str,
    forecast: object,
    attempt: int = 1,
    status: str = "ok",
) -> None:
    ledger.record_model_forecast(
        run_id,
        model_id,
        attempt,
        "2026-10-12T00:00:00Z",
        "2026-10-12T00:01:00Z",
        status,
        forecast if status == "ok" else None,
        None,
        None,
        None,
        None,
        None if status == "ok" else "boom",
    )


def serve(httpx_mock: HTTPXMock, *posts: dict[str, Any]) -> None:
    for post in posts:
        httpx_mock.add_response(
            url=f"https://www.metaculus.com/api/posts/{post['id']}/", json=post
        )


async def run_update(
    ledger: Ledger, tmp_path: Path, pool_path: Path, now: datetime = NOW
) -> weights_module.UpdateSummary:
    return await update_weights(ledger, tmp_path, pool_path, "test-token", now=now)


def only_score(ledger: Ledger) -> dict[str, Any]:
    (row,) = ledger.get_model_scores()
    return row


# -- scoring, one question type at a time -------------------------------------


async def test_binary_baseline_score(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, binary_post(), {"model-a": 0.2})
    serve(httpx_mock, binary_post())

    await run_update(ledger, tmp_path, pool_path)

    # Resolved "no", P(yes)=0.2 so P(outcome)=0.8; uniform gives 0.5.
    # By hand: ln(0.8) - ln(0.5) = ln(1.6) = 0.47000362924573563
    row = only_score(ledger)
    assert row["score"] == pytest.approx(0.47000362924573563)
    assert row["method"] == "baseline"
    assert row["model_id"] == "model-a"


async def test_multiple_choice_baseline_score(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, mc_post(), {"model-a": {"Low": 0.2, "Medium": 0.5, "High": 0.3}})
    serve(httpx_mock, mc_post())

    await run_update(ledger, tmp_path, pool_path)

    # Resolved "Medium" at 0.5 against 1/3 for three options.
    # By hand: ln(0.5) - ln(1/3) = ln(1.5) = 0.4054651081081644
    assert only_score(ledger)["score"] == pytest.approx(0.4054651081081644)


async def test_numeric_score_uses_the_bin_holding_the_resolution(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, numeric_post(), {"model-a": numeric_cdf(0.1)})
    serve(httpx_mock, numeric_post())

    await run_update(ledger, tmp_path, pool_path)

    # Grid 2540 + 4.3 * i, resolution 2664.0: 2660.4 <= 2664.0 < 2664.7, bin 28,
    # mass 0.1. Both bounds are open, so a uniform forecast has 202 cells.
    # By hand: ln(0.1) - ln(1/202) = ln(20.2) = 3.005682604407159
    assert only_score(ledger)["score"] == pytest.approx(3.005682604407159, abs=1e-9)


async def test_discrete_score_uses_the_forecasts_own_grid(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, discrete_post(), {"model-a": DISCRETE_CDF})
    serve(httpx_mock, discrete_post())

    await run_update(ledger, tmp_path, pool_path)

    # 5 outcomes (16..20), edges 15.5..20.5, resolution 19 is the 4th bin with
    # mass 0.3. Lower bound closed, upper open: 5 bins + 1 tail = 6 cells.
    # By hand: ln(0.3) - ln(1/6) = ln(1.8) = 0.587786664902119
    assert only_score(ledger)["score"] == pytest.approx(0.587786664902119)


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        # Upper tail mass is 1 - 0.97 = 0.03. By hand: ln(0.03 * 6)
        ("25.0", -1.7147984280919266),
        ("above_upper_bound", -1.7147984280919266),
        # The lowest edge belongs to the first bin, mass 0.1. By hand: ln(0.6)
        ("15.5", -0.5108256237659907),
    ],
)
async def test_resolutions_beyond_the_range_use_the_tail_mass(
    resolution: str,
    expected: float,
    ledger: Ledger,
    tmp_path: Path,
    pool_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    post = with_resolution(discrete_post(), resolution)
    seed(ledger, post, {"model-a": DISCRETE_CDF})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)

    assert only_score(ledger)["score"] == pytest.approx(expected)


async def test_date_score_reads_iso_grid_and_resolution(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    # Real date post (grid of ISO timestamps, 4h26m42s apart) with a resolution
    # added: the API shows none for any date post in reach.
    post = with_resolution(date_post(), "2026-03-27T00:00:00Z")
    masses = [0.05 if i == 10 else 0.9 / 199 for i in range(200)]
    seed(ledger, post, {"model-a": cdf_from_masses(0.0, masses)})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)

    # Grid point 10 is 2026-03-26T20:27Z and point 11 is 00:53:42Z the next day,
    # so the resolution is in bin 10 (mass 0.05). Lower bound closed, upper
    # open: 200 bins + 1 tail = 201 cells. By hand: ln(0.05 * 201) = ln(10.05)
    assert only_score(ledger)["score"] == pytest.approx(2.307572634505085)


async def test_mass_is_floored_and_score_clipped(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, numeric_post(), {"model-a": numeric_cdf(0.0)})
    seed(ledger, binary_post(), {"model-a": 1.0})
    seed(ledger, discrete_post(), {"model-a": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
    serve(httpx_mock, binary_post(), discrete_post(), numeric_post())

    await run_update(ledger, tmp_path, pool_path)

    scores = {r["question_id"]: r["score"] for r in ledger.get_model_scores()}
    # Mass 0 is floored to 1e-4. By hand: ln(1e-4 * 202) = -3.9020726745749776
    assert scores["metaculus:46132"] == pytest.approx(-3.9020726745749776)
    # P(outcome)=0 on the binary: ln(1e-4 / 0.5) = -8.5 is clipped to -5.
    assert scores["metaculus:46130"] == -5.0
    # Floored mass 1e-4 against 6 cells: ln(6e-4) = -7.4 is clipped to -5.
    assert scores["metaculus:46074"] == -5.0


async def test_score_above_the_clip_is_clipped(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, numeric_post(), {"model-a": numeric_cdf(0.9)})
    serve(httpx_mock, numeric_post())

    await run_update(ledger, tmp_path, pool_path)

    # By hand: ln(0.9 * 202) = 5.2 -> 5.0
    assert only_score(ledger)["score"] == 5.0


# -- peer score -----------------------------------------------------------------


def bot_forecast(post: dict[str, Any]) -> Any:
    """The bot's own last forecast on `post`, in the shape the ledger stores."""
    question = post["question"]
    values = question["my_forecasts"]["latest"]["forecast_values"]
    if question["type"] == "binary":
        return values[1]
    if question["type"] == "multiple_choice":
        return dict(zip(question["options"], values, strict=True))
    return values


@pytest.mark.parametrize(
    ("post_loader", "forecast", "expected"),
    [
        # Resolved "no": the bot gave P(no) 0.815 and scored spot_peer_score
        # -3.296704306198748, so the field's mean log score is
        # F = ln(0.815) + 0.03296704306198748. The model gave P(no) 0.8.
        # By hand: ln(0.8) - F = ln(0.8 / 0.815) - 0.03296704306198748
        (binary_post, 0.2, -0.05154342863492278),
        # Bot 0.57 on "Medium", spot_peer_score 15.591379491753045; model 0.5.
        # By hand: ln(0.5 / 0.57) + 0.15591379491753045
        (mc_post, {"Low": 0.2, "Medium": 0.5, "High": 0.3}, 0.024885532511126324),
        # Bot mass in bin 28 is 0.2480915 - 0.2285696666 = 0.0195218334,
        # spot_peer_score 48.81254269507833 (halved by Metaculus on continuous
        # questions, so F uses / 50); model mass 0.1.
        # By hand: ln(0.1 / 0.0195218334) + 0.9762508539015666
        (numeric_post, numeric_cdf(0.1), 2.6098875391349834),
        # Bot mass in bin 3 is 0.9805166666 - 0.9424268426 = 0.038089824,
        # spot_peer_score 0.711797552746385 (/ 50, continuous); model mass 0.3.
        # By hand: ln(0.3 / 0.038089824) + 0.0142359510549277
        (discrete_post, DISCRETE_CDF, 2.0780712658666287),
    ],
    ids=["binary", "multiple_choice", "numeric", "discrete"],
)
async def test_score_is_relative_to_the_fields_mean_log_score(
    post_loader: Callable[..., dict[str, Any]],
    forecast: Any,
    expected: float,
    ledger: Ledger,
    tmp_path: Path,
    pool_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    post = post_loader(bot_scores=True)
    seed(ledger, post, {"model-a": forecast})
    serve(httpx_mock, post)

    summary = await run_update(ledger, tmp_path, pool_path)

    row = only_score(ledger)
    assert row["score"] == pytest.approx(expected, abs=1e-9)
    assert row["method"] == "peer"
    assert summary.by_method == {"peer": 1}


async def test_the_option_resolved_is_matched_to_the_bots_probability_by_name(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    # The fixture's resolution is changed to "Low" (the bot gave it 0.365; its
    # peer score is kept as is, which is all this test needs).
    post = with_resolution(mc_post(bot_scores=True), "Low")
    seed(ledger, post, {"model-a": {"Low": 0.2, "Medium": 0.5, "High": 0.3}})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)

    # By hand: ln(0.2 / 0.365) + 0.15591379491753045
    assert only_score(ledger)["score"] == pytest.approx(-0.44566619211692426)


async def test_the_bots_mass_on_the_outcome_is_floored_before_the_field_score(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    post = binary_post(bot_scores=True)
    post["question"]["my_forecasts"]["latest"]["forecast_values"] = [0.0, 1.0]
    post["question"]["my_forecasts"]["score_data"]["spot_peer_score"] = -500.0
    seed(ledger, post, {"model-a": 0.2})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)

    # The bot gave "no" probability 0, floored to 1e-4: F = ln(1e-4) + 5.
    # By hand: ln(0.8) - F = 3.987196820661972
    row = only_score(ledger)
    assert row["method"] == "peer"
    assert row["score"] == pytest.approx(3.987196820661972)


@pytest.mark.parametrize(
    ("post_loader", "peer_score", "scale"),
    [
        (binary_post, -3.296704306198748, 100),
        (mc_post, 15.591379491753045, 100),
        (numeric_post, 48.81254269507833, 50),
        (discrete_post, 0.711797552746385, 50),
    ],
    ids=["binary", "multiple_choice", "numeric", "discrete"],
)
async def test_a_model_forecasting_like_the_bot_scores_its_peer_score(
    post_loader: Callable[..., dict[str, Any]],
    peer_score: float,
    scale: int,
    ledger: Ledger,
    tmp_path: Path,
    pool_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    post = post_loader(bot_scores=True)
    seed(ledger, post, {"model-a": bot_forecast(post)})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)

    # F = ln(m_bot) - peer / scale, so ln(m_bot) - F = peer / scale.
    assert only_score(ledger)["score"] == pytest.approx(peer_score / scale)


def without_peer_score(post: dict[str, Any], how: str) -> dict[str, Any]:
    post = copy.deepcopy(post)
    mine = post["question"]["my_forecasts"]
    if how == "no_score_data":
        mine["score_data"] = {}
    elif how == "no_peer_score":
        del mine["score_data"]["spot_peer_score"]
    elif how == "no_latest_forecast":
        del mine["latest"]
    elif how == "bot_forecast_of_the_wrong_length":
        mine["latest"]["forecast_values"] = [0.2, 0.3, 0.5]
    elif how == "no_my_forecasts":
        del post["question"]["my_forecasts"]
    return post


@pytest.mark.parametrize(
    "how",
    [
        "no_score_data",
        "no_peer_score",
        "no_latest_forecast",
        "bot_forecast_of_the_wrong_length",
        "no_my_forecasts",
    ],
)
async def test_without_a_usable_peer_score_the_score_is_the_baseline(
    how: str,
    ledger: Ledger,
    tmp_path: Path,
    pool_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    post = without_peer_score(binary_post(bot_scores=True), how)
    seed(ledger, post, {"model-a": 0.2})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)

    # By hand: ln(0.8) - ln(0.5) = ln(1.6)
    row = only_score(ledger)
    assert row["score"] == pytest.approx(0.47000362924573563)
    assert row["method"] == "baseline"


async def test_a_peer_score_that_arrives_later_replaces_the_baseline_score(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    unscored = without_peer_score(binary_post(bot_scores=True), "no_score_data")
    seed(ledger, unscored, {"model-a": 0.2})
    serve(httpx_mock, unscored, binary_post(bot_scores=True))

    first = await run_update(ledger, tmp_path, pool_path)
    assert only_score(ledger)["method"] == "baseline"
    assert first.by_method == {"baseline": 1}

    await run_update(ledger, tmp_path, pool_path, NOW + timedelta(days=3))
    assert len(httpx_mock.get_requests()) == 1  # not yet 7 days after the fetch

    later = await run_update(ledger, tmp_path, pool_path, NOW + timedelta(days=8))
    row = only_score(ledger)
    assert (row["method"], later.by_method) == ("peer", {"peer": 1})
    assert row["score"] == pytest.approx(-0.05154342863492278)

    final = await run_update(ledger, tmp_path, pool_path, NOW + timedelta(days=40))
    assert (final.fetched, final.scored) == (0, 0)  # peer score cached: final
    assert len(httpx_mock.get_requests()) == 2


# -- which forecasts count ------------------------------------------------------


async def test_each_run_scores_the_models_last_ok_forecast(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    run_id = seed(ledger, binary_post(), {"model-a": 0.9})
    record_forecast(ledger, run_id, "model-a", 0.2, attempt=2)
    record_forecast(ledger, run_id, "model-a", None, attempt=3, status="failed")
    serve(httpx_mock, binary_post())

    await run_update(ledger, tmp_path, pool_path)

    assert only_score(ledger)["score"] == pytest.approx(0.47000362924573563)


async def test_a_forecast_of_the_wrong_shape_is_skipped_and_counted(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, binary_post(), {"model-a": [0.1, 0.9], "model-b": 0.2})
    serve(httpx_mock, binary_post())

    summary = await run_update(ledger, tmp_path, pool_path)

    assert [r["model_id"] for r in ledger.get_model_scores()] == ["model-b"]
    assert summary.skipped == {"forecast_does_not_fit": 1}


async def test_a_models_reruns_count_once_per_question(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, binary_post(), {"model-a": 0.9})
    seed(ledger, binary_post(), {"model-a": 0.2})
    serve(httpx_mock, binary_post())

    summary = await run_update(ledger, tmp_path, pool_path)

    assert len(ledger.get_model_scores()) == 2
    assert summary.n_scored["model-a"] == 1


# -- caching and refetching ---------------------------------------------------


async def test_a_resolved_question_is_fetched_once(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    post = binary_post(bot_scores=True)
    seed(ledger, post, {"model-a": 0.2})
    serve(httpx_mock, post)

    first = await run_update(ledger, tmp_path, pool_path)
    second = await run_update(ledger, tmp_path, pool_path, NOW + timedelta(days=30))

    assert (first.fetched, first.scored) == (1, 1)
    assert (second.fetched, second.scored) == (0, 0)
    assert len(httpx_mock.get_requests()) == 1


async def test_request_carries_the_token_and_a_browser_user_agent(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, binary_post(), {"model-a": 0.2})
    serve(httpx_mock, binary_post())

    await run_update(ledger, tmp_path, pool_path)

    (request,) = httpx_mock.get_requests()
    assert request.headers["authorization"] == "Token test-token"
    assert request.headers["user-agent"].startswith("Mozilla/5.0")


async def test_a_question_not_yet_closed_is_not_fetched(
    ledger: Ledger, tmp_path: Path, pool_path: Path
) -> None:
    seed(ledger, binary_post(), {"model-a": 0.2}, close_time="2026-10-25T00:00:00Z")

    summary = await run_update(ledger, tmp_path, pool_path)

    assert summary.fetched == 0


async def test_an_unresolved_post_is_refetched_after_its_scheduled_resolve_time(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    open_post = copy.deepcopy(binary_post())
    open_post["question"]["status"] = "closed"
    open_post["question"]["resolution"] = None
    seed(ledger, open_post, {"model-a": 0.2})
    scheduled = datetime(2026, 10, 17, 23, 59, tzinfo=UTC)  # in the fixture
    serve(httpx_mock, open_post, open_post, binary_post())

    day_before = scheduled - timedelta(days=1)
    await run_update(ledger, tmp_path, pool_path, day_before)  # first fetch
    await run_update(ledger, tmp_path, pool_path, day_before + timedelta(hours=1))
    assert len(httpx_mock.get_requests()) == 1  # not due yet

    await run_update(ledger, tmp_path, pool_path, scheduled + timedelta(hours=1))
    assert len(httpx_mock.get_requests()) == 2  # scheduled time has passed
    assert ledger.get_model_scores() == []

    await run_update(ledger, tmp_path, pool_path, scheduled + timedelta(days=2))
    assert len(httpx_mock.get_requests()) == 2  # fetched since: wait for 7 days

    summary = await run_update(
        ledger, tmp_path, pool_path, scheduled + timedelta(days=9)
    )
    assert len(httpx_mock.get_requests()) == 3
    assert summary.scored == 1  # it resolved meanwhile


async def test_a_resolved_post_with_a_null_resolution_is_not_scored(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    post = load_post("metaculus_post_45941_unresolved.json")
    seed(ledger, post, {"model-a": DISCRETE_CDF[:5]})
    serve(httpx_mock, post)

    summary = await run_update(ledger, tmp_path, pool_path)

    cached = ledger.get_resolution("metaculus:46123")
    assert cached is not None
    assert cached["status"] == "pending"
    assert summary.scored == 0


async def test_an_annulled_question_is_cached_for_good_and_not_scored(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    post = with_resolution(binary_post(), "annulled")
    seed(ledger, post, {"model-a": 0.2})
    serve(httpx_mock, post)

    await run_update(ledger, tmp_path, pool_path)
    await run_update(ledger, tmp_path, pool_path, NOW + timedelta(days=30))

    cached = ledger.get_resolution("metaculus:46130")
    assert cached is not None
    assert cached["status"] == "annulled"
    assert ledger.get_model_scores() == []
    assert len(httpx_mock.get_requests()) == 1


async def test_a_post_for_a_different_question_is_never_scored(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    ledger_view = binary_post()
    ledger_view["question"]["id"] = 99999  # the ledger keys this post as 99999
    seed(ledger, ledger_view, {"model-a": 0.2})
    serve(httpx_mock, binary_post())  # but the API says question 46130

    await run_update(ledger, tmp_path, pool_path)

    cached = ledger.get_resolution("metaculus:99999")
    assert cached is not None
    assert cached["status"] == "pending"
    assert ledger.get_model_scores() == []


async def test_a_deleted_post_is_skipped_without_failing_the_update(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, binary_post(), {"model-a": 0.2})
    seed(ledger, mc_post(), {"model-a": {"Low": 0.2, "Medium": 0.5, "High": 0.3}})
    httpx_mock.add_response(
        url=f"https://www.metaculus.com/api/posts/{binary_post()['id']}/",
        status_code=404,
    )
    serve(httpx_mock, mc_post())

    summary = await run_update(ledger, tmp_path, pool_path)

    assert summary.skipped == {"post_not_found": 1}
    deleted = ledger.get_resolution("metaculus:46130")
    assert deleted is not None
    assert deleted["status"] == "pending"
    assert [r["question_id"] for r in ledger.get_model_scores()] == ["metaculus:46086"]


async def test_a_server_error_aborts_but_keeps_what_was_already_cached(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    seed(ledger, binary_post(), {"model-a": 0.2})
    seed(ledger, mc_post(), {"model-a": {"Low": 0.2, "Medium": 0.5, "High": 0.3}})
    serve(httpx_mock, mc_post())  # post 45904 sorts first by question id: 46086
    httpx_mock.add_response(
        url=f"https://www.metaculus.com/api/posts/{binary_post()['id']}/",
        status_code=503,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await run_update(ledger, tmp_path, pool_path)

    assert ledger.get_resolution("metaculus:46086") is not None
    assert ledger.get_resolution("metaculus:46130") is None
    assert not (tmp_path / "weights.json").exists()


async def test_fetching_without_a_token_fails_but_idle_update_does_not(
    ledger: Ledger, tmp_path: Path, pool_path: Path
) -> None:
    summary = await update_weights(ledger, tmp_path, pool_path, None, now=NOW)
    assert summary.fetched == 0

    seed(ledger, binary_post(), {"model-a": 0.2})
    with pytest.raises(RuntimeError, match="METACULUS_TOKEN"):
        await update_weights(ledger, tmp_path, pool_path, None, now=NOW)


# -- weights --------------------------------------------------------------------


def test_compute_weights_shrinks_normalizes_and_floors() -> None:
    scores = {
        "m1": [0.5] * 10,  # s = 0.5 * 10 / 20 = 0.25
        "m2": [-0.2] * 30,  # s = -0.2 * 30 / 40 = -0.15
        "m3": [2.0] * 9,  # fewer than 10 scores: neutral, s = 0
        "off": [3.0] * 50,  # not enabled: ignored
    }

    weights, n_scored = compute_weights(scores, ["m1", "m2", "m3", "m4"])

    # By hand: raw = exp(s / 0.2) = 3.4903429574618414, 0.47236655274101474, 1, 1;
    # their mean is 1.490677377550714, so normalized 2.3414475929035135,
    # 0.31688047316928203 (floored to 0.5), 0.6708359669636023, 0.6708359669636023.
    assert weights == {
        "m1": pytest.approx(2.3414475929035135),
        "m2": 0.5,
        "m3": pytest.approx(0.6708359669636023),
        "m4": pytest.approx(0.6708359669636023),
    }
    assert sum(weights.values()) / 4 == pytest.approx(1.0457798817076795)
    assert n_scored == {"m1": 10, "m2": 30, "m3": 9, "m4": 0}


def test_compute_weights_with_no_scores_is_uniform() -> None:
    weights, n_scored = compute_weights({}, ["a", "b", "c"])

    assert weights == {"a": 1.0, "b": 1.0, "c": 1.0}
    assert n_scored == {"a": 0, "b": 0, "c": 0}


def binary_variant(post_id: int) -> dict[str, Any]:
    """The real binary post under a fresh post and question id (resolves "no")."""
    post = binary_post()
    post["id"] = post["question"]["post_id"] = post_id
    post["question"]["id"] = post_id + 1000
    return post


async def test_update_writes_weights_for_the_enabled_pool(
    ledger: Ledger,
    tmp_path: Path,
    pool_path: Path,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    posts = [binary_variant(50000 + i) for i in range(10)]
    for i, post in enumerate(posts):
        forecasts: dict[str, object] = {
            "model-a": 0.2,  # P(no) = 0.8 against 0.5: +ln(1.6)
            "model-b": 0.8,  # P(no) = 0.2 against 0.5: ln(0.4)
            "model-off": 0.2,
        }
        if i < 9:
            forecasts["model-c"] = 0.2  # nine scores: still neutral
        seed(ledger, post, forecasts)
    serve(httpx_mock, *posts)

    with caplog.at_level(logging.INFO):
        summary = await run_update(ledger, tmp_path, pool_path)

    # By hand: model-a mean 0.47000362924573563, s = mean * 10 / 20 = 0.2350018...,
    # model-b mean ln(0.4) = -0.916290731874155, s = -0.4581453...; raw weights
    # exp(s / 0.2) = 3.23817232401242 and 0.10119288512538822; model-c and
    # model-e are neutral (1.0). Mean over the four enabled models is
    # 1.334841302284452. model-b's 0.0758... is floored to 0.5.
    assert load_weights(tmp_path) == {
        "model-a": pytest.approx(2.4258856228606356),
        "model-b": 0.5,
        "model-c": pytest.approx(0.7491527257124847),
        "model-e": pytest.approx(0.7491527257124847),
    }
    written = orjson.loads((tmp_path / "weights.json").read_bytes())
    assert written["n_resolved"] == {
        "model-a": 10,
        "model-b": 10,
        "model-c": 9,
        "model-e": 0,
    }
    assert written["updated_at"] == "2026-10-24T12:00:00Z"
    assert summary.weights == load_weights(tmp_path)
    assert (summary.fetched, summary.scored) == (10, 39)
    messages = [r.getMessage() for r in caplog.records if r.name == "betomcat.weights"]
    assert messages == [
        "weight update: fetched=10 scored=39 by_method={'baseline': 39} "
        "skipped={} models=4 models_with_enough_scores=2"
    ]


async def test_only_the_latest_run_on_a_question_feeds_the_weights(
    ledger: Ledger, tmp_path: Path, pool_path: Path, httpx_mock: HTTPXMock
) -> None:
    posts = [binary_variant(51000 + i) for i in range(10)]
    for post in posts:
        seed(ledger, post, {"model-a": 0.9})  # an earlier run: ln(0.1 / 0.5)
        seed(ledger, post, {"model-a": 0.2})  # the rerun that counts: ln(1.6)
    serve(httpx_mock, *posts)

    await run_update(ledger, tmp_path, pool_path)

    # By hand: ten scores of 0.47000362924573563 give s = 0.2350018...,
    # raw 3.23817232401242 against 1.0 for the other three models, so the
    # mean is 1.559543... and model-a gets 2.076359648833563.
    assert len(ledger.get_model_scores()) == 20
    assert load_weights(tmp_path) == {
        "model-a": pytest.approx(2.076359648833563),
        "model-b": pytest.approx(0.6412134503888123),
        "model-c": pytest.approx(0.6412134503888123),
        "model-e": pytest.approx(0.6412134503888123),
    }


def test_weights_updated_today_compares_the_utc_date(tmp_path: Path) -> None:
    assert not weights_updated_today(tmp_path, NOW)  # no file

    (tmp_path / "weights.json").write_bytes(
        orjson.dumps({"updated_at": "2026-10-24T01:00:00Z", "weights": {}})
    )
    assert weights_updated_today(tmp_path, NOW)
    assert weights_updated_today(tmp_path, NOW.replace(hour=23, minute=59))
    assert not weights_updated_today(tmp_path, NOW + timedelta(hours=12))

    (tmp_path / "weights.json").write_bytes(b"not json")
    assert not weights_updated_today(tmp_path, NOW)
