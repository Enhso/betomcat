"""Weighted-average reconciliation (spec s5/s6). Mechanical, no override.

Each function takes the frozen per-model weights from the draw (never
re-reads `weights.json`) and returns both the combined forecast and a
human-readable arithmetic string, used verbatim in the private comment
(contracts.md s E item 4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryReconciliation:
    probability: float
    arithmetic: str


@dataclass(frozen=True)
class MultipleChoiceReconciliation:
    options: dict[str, float]
    arithmetic: str


@dataclass(frozen=True)
class NumericReconciliation:
    cdf: list[float]
    arithmetic: str


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    lo, hi = bounds
    return min(hi, max(lo, value))


def reconcile_binary(
    forecasts: list[tuple[str, float, float]],
    clamp: tuple[float, float] = (0.01, 0.99),
) -> BinaryReconciliation:
    """Weighted-average a binary ensemble.

    Args:
        forecasts: `(model_id, probability, weight)` triples.
        clamp: `(min, max)` bounds applied to the final probability.

    Returns:
        The clamped weighted-average probability and its arithmetic string.

    Raises:
        ValueError: If `forecasts` is empty.
    """
    if not forecasts:
        raise ValueError("reconcile_binary requires at least one forecast")
    if len(forecasts) == 1:
        model_id, p, _w = forecasts[0]
        clamped = _clamp(p, clamp)
        return BinaryReconciliation(
            probability=clamped,
            arithmetic=f"{model_id}: {p:.4f} (single forecast, used as-is)",
        )

    total_weight = sum(w for _, _, w in forecasts)
    weighted_sum = sum(p * w for _, p, w in forecasts)
    probability = weighted_sum / total_weight
    clamped = _clamp(probability, clamp)

    terms = " + ".join(f"{p:.4f}*{w:.4f}" for _, p, w in forecasts)
    weight_terms = "+".join(f"{w:.4f}" for _, _, w in forecasts)
    arithmetic = f"({terms}) / ({weight_terms}) = {clamped:.4f}"
    return BinaryReconciliation(probability=clamped, arithmetic=arithmetic)


def reconcile_multiple_choice(
    forecasts: list[tuple[str, dict[str, float], float]],
    floor: float = 0.001,
) -> MultipleChoiceReconciliation:
    """Weighted-average a multiple-choice ensemble, floored and renormalised.

    Args:
        forecasts: `(model_id, {option: probability}, weight)` triples. Every
            forecast must carry the same option set.
        floor: Minimum probability assigned to any option before renormalising.

    Returns:
        The combined per-option probabilities (summing to 1) and the
        arithmetic string.

    Raises:
        ValueError: If `forecasts` is empty or option sets disagree.
    """
    if not forecasts:
        raise ValueError("reconcile_multiple_choice requires at least one forecast")
    options = list(forecasts[0][1].keys())
    for model_id, opts, _w in forecasts:
        if set(opts.keys()) != set(options):
            raise ValueError(
                f"option set mismatch: {model_id} has {sorted(opts)}, "
                f"expected {sorted(options)}"
            )

    if len(forecasts) == 1:
        model_id, opts, _w = forecasts[0]
        return MultipleChoiceReconciliation(
            options=dict(opts),
            arithmetic=f"{model_id}: {opts} (single forecast, used as-is)",
        )

    total_weight = sum(w for _, _, w in forecasts)
    raw = {
        option: sum(opts[option] * w for _, opts, w in forecasts) / total_weight
        for option in options
    }
    floored = {option: max(value, floor) for option, value in raw.items()}
    floored_total = sum(floored.values())
    normalised = {option: value / floored_total for option, value in floored.items()}

    per_model = "; ".join(
        f"{model_id} (w={w:.4f}): {opts}" for model_id, opts, w in forecasts
    )
    arithmetic = (
        f"weighted mean of [{per_model}], floored at {floor}, "
        f"renormalised -> {normalised}"
    )
    return MultipleChoiceReconciliation(options=normalised, arithmetic=arithmetic)


def reconcile_numeric(
    forecasts: list[tuple[str, list[float], float]],
) -> NumericReconciliation:
    """Pointwise-weighted-average a numeric/discrete/date CDF ensemble.

    A convex combination of valid Metaculus CDFs (each non-decreasing, in
    `[0, 1]`) stays a valid CDF, so no post-hoc renormalisation is needed.

    Args:
        forecasts: `(model_id, cdf_probabilities, weight)` triples, where
            `cdf_probabilities` are the CDF y-values at the question's fixed
            evaluation points (same length and locations for every model).

    Returns:
        The combined CDF and the arithmetic string.

    Raises:
        ValueError: If `forecasts` is empty or CDF lengths disagree.
    """
    if not forecasts:
        raise ValueError("reconcile_numeric requires at least one forecast")
    size = len(forecasts[0][1])
    for model_id, cdf, _w in forecasts:
        if len(cdf) != size:
            raise ValueError(
                f"CDF length mismatch: {model_id} has {len(cdf)}, expected {size}"
            )

    if len(forecasts) == 1:
        model_id, cdf, _w = forecasts[0]
        return NumericReconciliation(
            cdf=list(cdf),
            arithmetic=f"{model_id}: cdf[{size} pts] (single forecast, used as-is)",
        )

    total_weight = sum(w for _, _, w in forecasts)
    combined = [
        sum(cdf[i] * w for _, cdf, w in forecasts) / total_weight for i in range(size)
    ]

    weight_terms = ", ".join(f"{model_id}=w{w:.4f}" for model_id, _, w in forecasts)
    arithmetic = (
        f"pointwise weighted mean of {len(forecasts)} CDFs [{size} pts each] "
        f"({weight_terms}) / {total_weight:.4f}"
    )
    return NumericReconciliation(cdf=combined, arithmetic=arithmetic)
