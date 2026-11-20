"""Tests for the tail backtest, on synthetic data only."""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import pytest

from betomcat.backtest import (
    BASE_VARIANT,
    build_cdf,
    extremize,
    infer_zero_point,
    load_cases,
    mix_cdf,
    parse_percentiles,
    run_numeric,
    wide_cdf,
)
from betomcat.ledger import Ledger
from betomcat.reconcile import reconcile_numeric
from betomcat.weights import Scaling

LEVELS = (1, 5, 10, 20, 40, 50, 60, 80, 90, 95, 99)
MODELS = {"model-a": 1.0, "model-b": 2.0}
GRID = [i * 0.5 for i in range(201)]
SCALING: Scaling = {
    "type": "numeric",
    "options": None,
    "grid": GRID,
    "open_lower": True,
    "open_upper": True,
}


def response(low: float, high: float) -> str:
    """A model response with P1..P99 spread linearly between `low` and `high`."""
    lines = [
        f"Percentile {level}: {low + (high - low) * level / 100:.2f}"
        for level in LEVELS
    ]
    return "Reasoning text.\n" + "\n".join(lines)


def log_grid(zero_point: float, low: float, high: float, size: int) -> list[float]:
    ratio = (high - zero_point) / (low - zero_point)
    return [
        low + (high - low) * (ratio ** (i / (size - 1)) - 1) / (ratio - 1)
        for i in range(size)
    ]


def build_ledger(
    path: Path,
    resolution: str,
    corrupt_model_cdf: bool = False,
    corrupt_submission: bool = False,
) -> None:
    """One resolved open/open question with two models and a final submission."""
    texts = {"model-a": response(20, 60), "model-b": response(25, 65)}
    stored = {
        m: build_cdf(parse_percentiles(t), SCALING, 0, 1) for m, t in texts.items()
    }
    combined = reconcile_numeric([(m, stored[m], w) for m, w in MODELS.items()]).cdf
    if corrupt_model_cdf:
        stored["model-a"] = wide_cdf(SCALING)
    if corrupt_submission:
        combined = wide_cdf(SCALING)
    with Ledger(path) as ledger:
        ledger.upsert_question(
            "metaculus:1", "t", "numeric", None, "https://x/questions/777/slug/"
        )
        run = ledger.start_run("metaculus:1")
        ledger.finish_run(run.id, "submitted", family_id="fam:one")
        ledger.record_draw(run.id, MODELS, 1.0, False)
        for model_id, text in texts.items():
            ledger.record_model_forecast(
                run.id, model_id, 1, "t0", "t1", "ok", stored[model_id], text,
                0.0, 0, 0, None,
            )  # fmt: skip
        ledger.record_submission(run.id, "final", combined, False)
        ledger.upsert_resolution(
            "metaculus:1", "resolved", resolution, -1.0, SCALING, None, "t2"
        )


def test_zero_point_round_trips_on_log_grid_and_is_none_on_linear() -> None:
    grid = log_grid(-3.0, 2.0, 400.0, 201)
    assert infer_zero_point(grid) == pytest.approx(-3.0, abs=1e-6)
    assert infer_zero_point(GRID) is None


def test_outcome_above_bound_scores_minus_106_5_baseline_and_wide_variant_near_zero(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite"
    build_ledger(ledger_path, "120.0")
    cases, _, _ = load_cases(ledger_path)

    gates, rows, failures, _ = run_numeric(cases)

    by_variant = {r.variant: r for r in rows}
    assert gates[0].passed
    assert not failures
    assert by_variant[BASE_VARIANT].oob == "1"
    assert by_variant[BASE_VARIANT].baseline == pytest.approx(-106.5, abs=0.2)
    assert by_variant["p10_p90"].baseline == pytest.approx(0.0, abs=2.0)
    assert by_variant["p10_p90"].peer > by_variant[BASE_VARIANT].peer


def test_gate_passes_on_clean_run_and_flags_planted_faults(tmp_path: Path) -> None:
    clean, bad_model, bad_submission = (tmp_path / n for n in ("c", "m", "s"))
    build_ledger(clean, "30.0")
    build_ledger(bad_model, "30.0", corrupt_model_cdf=True)
    build_ledger(bad_submission, "30.0", corrupt_submission=True)

    reasons = [
        run_numeric(load_cases(p)[0])[0][0].reason
        for p in (clean, bad_model, bad_submission)
    ]

    assert reasons[0] == ""
    assert "model_cdf_mismatch" in reasons[1]
    assert "submission_mismatch" in reasons[2]


def test_failed_gate_question_is_left_out_of_scores(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    build_ledger(path, "30.0", corrupt_submission=True)

    gates, rows, _, _ = run_numeric(load_cases(path)[0])

    assert not gates[0].passed
    assert rows == []


def test_mixed_cdf_is_non_decreasing_with_right_endpoint_masses() -> None:
    today = build_cdf(parse_percentiles(response(20, 60)), SCALING, 0, 1)
    wide = wide_cdf(SCALING)

    mixed = mix_cdf(today, wide, 0.2)

    assert all(b >= a for a, b in itertools.pairwise(mixed))
    assert wide[0] == pytest.approx(0.05)
    assert 1 - wide[-1] == pytest.approx(0.05)
    assert mixed[0] == pytest.approx(0.8 * today[0] + 0.2 * 0.05)
    assert 1 - mixed[-1] == pytest.approx(0.8 * (1 - today[-1]) + 0.2 * 0.05)


def test_wide_cdf_puts_nothing_beyond_closed_bounds() -> None:
    closed: Scaling = {**SCALING, "open_lower": False, "open_upper": False}

    wide = wide_cdf(closed)

    assert wide[0] == 0.0
    assert wide[-1] == pytest.approx(1.0)
    assert wide[1] == pytest.approx(1 / 200)


def test_extremize_pushes_away_from_half_and_clamps() -> None:
    assert extremize(0.3, 1.0) == pytest.approx(0.3)
    assert extremize(0.3, 2.0) < 0.3
    assert extremize(0.5, 2.0) == pytest.approx(0.5)
    assert extremize(0.02, 2.0) == 0.01
    assert math.isclose(extremize(0.98, 2.0), 0.99)
