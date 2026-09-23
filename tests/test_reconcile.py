"""Tests for weighted-average reconciliation (spec s5/s6)."""

from __future__ import annotations

import pytest

from betomcat.reconcile import (
    reconcile_binary,
    reconcile_multiple_choice,
    reconcile_numeric,
)


def test_reconcile_binary_weighted_average() -> None:
    result = reconcile_binary(
        [("model-a", 0.62, 1.10), ("model-b", 0.48, 0.95)],
        clamp=(0.01, 0.99),
    )

    expected = (0.62 * 1.10 + 0.48 * 0.95) / (1.10 + 0.95)
    assert result.probability == pytest.approx(expected)
    assert "0.6200*1.1000" in result.arithmetic
    assert "0.4800*0.9500" in result.arithmetic


def test_reconcile_binary_clamps_to_bounds() -> None:
    result = reconcile_binary(
        [("model-a", 0.999, 1.0), ("model-b", 0.999, 1.0)],
        clamp=(0.01, 0.99),
    )

    assert result.probability == pytest.approx(0.99)


def test_reconcile_binary_single_forecast_passthrough() -> None:
    result = reconcile_binary([("model-a", 0.73, 1.0)], clamp=(0.01, 0.99))

    assert result.probability == pytest.approx(0.73)
    assert "single forecast" in result.arithmetic


def test_reconcile_binary_requires_at_least_one_forecast() -> None:
    with pytest.raises(ValueError, match="at least one forecast"):
        reconcile_binary([], clamp=(0.01, 0.99))


def test_reconcile_multiple_choice_weighted_and_floored() -> None:
    result = reconcile_multiple_choice(
        [
            ("model-a", {"Yes": 0.9, "No": 0.1}, 2.0),
            ("model-b", {"Yes": 0.0, "No": 1.0}, 1.0),
        ],
        floor=0.001,
    )

    # raw Yes = (0.9*2 + 0.0*1) / 3 = 0.6, raw No = (0.1*2 + 1.0*1) / 3 = 0.4
    assert result.options["Yes"] == pytest.approx(0.6, abs=1e-6)
    assert result.options["No"] == pytest.approx(0.4, abs=1e-6)
    assert sum(result.options.values()) == pytest.approx(1.0)


def test_reconcile_multiple_choice_floor_and_renormalise() -> None:
    result = reconcile_multiple_choice(
        [
            ("model-a", {"A": 1.0, "B": 0.0, "C": 0.0}, 1.0),
        ],
        floor=0.001,
    )
    # Single forecast passes through as-is without flooring/renormalising.
    assert result.options == {"A": 1.0, "B": 0.0, "C": 0.0}

    result2 = reconcile_multiple_choice(
        [
            ("model-a", {"A": 1.0, "B": 0.0, "C": 0.0}, 1.0),
            ("model-b", {"A": 1.0, "B": 0.0, "C": 0.0}, 1.0),
        ],
        floor=0.001,
    )
    floored_total = 1.0 + 0.001 + 0.001
    assert result2.options["B"] == pytest.approx(0.001 / floored_total)
    assert result2.options["C"] == pytest.approx(0.001 / floored_total)
    assert sum(result2.options.values()) == pytest.approx(1.0)


def test_reconcile_multiple_choice_requires_matching_option_sets() -> None:
    with pytest.raises(ValueError, match="option set mismatch"):
        reconcile_multiple_choice(
            [
                ("model-a", {"A": 0.5, "B": 0.5}, 1.0),
                ("model-b", {"A": 0.5, "C": 0.5}, 1.0),
            ]
        )


def test_reconcile_numeric_pointwise_weighted_mean() -> None:
    result = reconcile_numeric(
        [
            ("model-a", [0.0, 0.5, 1.0], 1.0),
            ("model-b", [0.0, 0.7, 1.0], 3.0),
        ]
    )

    expected = [0.0, (0.5 * 1.0 + 0.7 * 3.0) / 4.0, 1.0]
    assert result.cdf == pytest.approx(expected)


def test_reconcile_numeric_single_forecast_passthrough() -> None:
    cdf = [0.0, 0.3, 0.6, 1.0]
    result = reconcile_numeric([("model-a", cdf, 1.0)])

    assert result.cdf == cdf
    assert "single forecast" in result.arithmetic


def test_reconcile_numeric_requires_matching_lengths() -> None:
    with pytest.raises(ValueError, match="CDF length mismatch"):
        reconcile_numeric(
            [
                ("model-a", [0.0, 1.0], 1.0),
                ("model-b", [0.0, 0.5, 1.0], 1.0),
            ]
        )
