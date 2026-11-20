"""Ledger-only backtest of the numeric prompt's tail percentiles.

Re-parses every resolved numeric/discrete question's stored model responses
the way `forecast.forecast_numeric` did, rebuilds each CDF with a narrower
set of declared percentiles (or mixed with a wide distribution), and scores
the outcome under each variant. A reproduction gate first checks that today's
variant (`p1_p99`) rebuilds what the ledger stored and what was submitted. A
second section tests whether binary forecasts were under- or over-confident.

Peer score (the decision metric) is exact because the field's forecasts stay
fixed: 50 * (ln m - F) with F the stored field log score. Baseline score
follows Metaculus's `scoring/score_math.py`: 50 * ln(m / b).

Run: `uv run python -m betomcat.backtest --ledger PATH --out DIR`. No network,
no LLM calls. The output holds private ledger data: keep it out of the repo.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import logging
import math
import random
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import orjson
from forecasting_tools.data_models.numeric_report import (
    NumericDistribution,
    Percentile,
)
from forecasting_tools.helpers.prediction_extractor import PredictionExtractor

from betomcat.reconcile import reconcile_numeric
from betomcat.weights import ABOVE_UPPER_BOUND, BELOW_LOWER_BOUND, Scaling, outcome_mass

logger = logging.getLogger(__name__)

OPEN_BOUND_MASS = 0.05
LEVEL_EPSILON = 1e-9
GATE_MAX_CDF_DIFF = 1e-3
GATE_MAX_PEER_DIFF = 0.2
CHECK_BASELINE = 50 * math.log(0.00594 / OPEN_BOUND_MASS)
CHECK_TOLERANCE = 0.2
DEFAULT_SEED = 20261120
DEFAULT_RESAMPLES = 10_000
CI_LEVEL = 0.90
BINARY_CLAMP = (0.01, 0.99)
EXTREMIZE_FACTORS = (1.0, 1.25, 1.5, 2.0)
CALIBRATION_EDGES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

BASE_VARIANT = "p1_p99"
FILTER_VARIANTS: dict[str, tuple[float, float]] = {
    BASE_VARIANT: (0.0, 1.0),
    "p5_p95": (0.05, 0.95),
    "p10_p90": (0.10, 0.90),
}
MIX_VARIANTS: dict[str, float] = {"mix_05": 0.05, "mix_10": 0.10, "mix_20": 0.20}
VARIANTS = (*FILTER_VARIANTS, *MIX_VARIANTS)

_POST_ID_RE = re.compile(r"/questions/(\d+)")


@dataclass(frozen=True)
class StoredForecast:
    """One ok model forecast as the ledger holds it."""

    row_id: int
    model_id: str
    rationale: str
    cdf: list[float]


@dataclass(frozen=True)
class Case:
    """A resolved numeric/discrete question with everything the backtest needs."""

    question_id: str
    post_id: str
    family: str
    kind: str
    scaling: Scaling
    resolution: str
    field_log_score: float
    forecasts: list[StoredForecast]
    weights: dict[str, float]
    submitted: list[float]


@dataclass(frozen=True)
class GateRow:
    """Reproduction-gate result for one question."""

    question_id: str
    post_id: str
    candidate: str
    model_cdf_diff: float
    model_peer_diff: float
    submitted_cdf_diff: float
    submitted_peer_diff: float
    reason: str

    @property
    def passed(self) -> bool:
        """True when no check failed."""
        return not self.reason


@dataclass(frozen=True)
class ScoreRow:
    """One question scored under one variant (or one binary variant)."""

    question_id: str
    post_id: str
    family: str
    kind: str
    oob: str
    variant: str
    mass: float
    peer: float
    baseline: float | None


@dataclass
class CaseResult:
    """A case's gate row, per-variant combined CDFs and per-variant failures."""

    gate: GateRow
    cdfs: dict[str, list[float]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


def infer_zero_point(grid: Sequence[float]) -> float | None:
    """The log-scale zero point of a Metaculus grid, or `None` if it is linear.

    A log grid satisfies `(g_i - z)` geometric in `i`, so three evenly spaced
    points `g0, gk, g2k` give `z = (g0*g2k - gk^2) / (g0 + g2k - 2*gk)`.

    Args:
        grid: The question's CDF evaluation points, ascending.

    Returns:
        The zero point, or `None` when the spacing is uniform.
    """
    k = (len(grid) - 1) // 2
    if k < 1:
        return None
    g0, gk, g2k = grid[0], grid[k], grid[2 * k]
    denominator = g0 + g2k - 2 * gk
    if abs(denominator) <= 1e-9 * abs(grid[-1] - grid[0]):
        return None
    return (g0 * g2k - gk * gk) / denominator


def parse_percentiles(text: str) -> list[Percentile]:
    """Declared percentiles in `text`, parsed with the bot's extractor helpers.

    Raises:
        ValueError: If no percentile line parses.
    """
    parsed = []
    for line in PredictionExtractor._get_percentile_lines(text):
        numbers = PredictionExtractor._parse_numbers_from_line(line)
        percentile = PredictionExtractor._percentile_from_numbers(numbers)
        if percentile:
            parsed.append(percentile)
    if not parsed:
        raise ValueError("no percentile lines in response")
    return parsed


def build_cdf(
    percentiles: Sequence[Percentile], scaling: Scaling, low: float, high: float
) -> list[float]:
    """The standardized CDF from the declared percentiles with level in [low, high].

    Raises:
        ValueError: If the filtered list is rejected by `NumericDistribution`.
    """
    grid = scaling["grid"]
    assert grid is not None
    kept = [
        p
        for p in percentiles
        if low - LEVEL_EPSILON <= p.percentile <= high + LEVEL_EPSILON
    ]
    distribution = NumericDistribution(
        declared_percentiles=kept,
        open_upper_bound=scaling["open_upper"],
        open_lower_bound=scaling["open_lower"],
        upper_bound=grid[-1],
        lower_bound=grid[0],
        zero_point=infer_zero_point(grid),
        cdf_size=len(grid),
    )
    return [point.percentile for point in distribution.get_cdf()]


def wide_cdf(scaling: Scaling) -> list[float]:
    """Metaculus's baseline CDF: 5% beyond each open bound, the rest uniform."""
    grid = scaling["grid"]
    assert grid is not None
    below = OPEN_BOUND_MASS if scaling["open_lower"] else 0.0
    above = OPEN_BOUND_MASS if scaling["open_upper"] else 0.0
    steps = len(grid) - 1
    return [below + (1 - below - above) * i / steps for i in range(len(grid))]


def mix_cdf(
    today: Sequence[float], wide: Sequence[float], weight: float
) -> list[float]:
    """`(1 - weight) * today + weight * wide`, pointwise."""
    return [(1 - weight) * t + weight * w for t, w in zip(today, wide, strict=True)]


def _outcome_beyond_bounds(resolution: str, grid: Sequence[float]) -> bool:
    if resolution in (BELOW_LOWER_BOUND, ABOVE_UPPER_BOUND):
        return True
    return not grid[0] <= float(resolution) <= grid[-1]


def _log_mass(mass: float | None) -> float:
    if mass is None or mass <= 0:
        raise ValueError(f"cannot score outcome mass {mass}")
    return math.log(mass)


def score_numeric(
    cdf: Sequence[float], resolution: str, scaling: Scaling, field_log_score: float
) -> tuple[float, float, float, bool]:
    """Mass, peer score, baseline score and out-of-bounds flag of one CDF.

    Raises:
        ValueError: If the CDF does not fit the grid or gives the outcome no mass.
    """
    grid = scaling["grid"]
    assert grid is not None
    mass = outcome_mass(list(cdf), resolution, scaling)
    log_mass = _log_mass(mass)
    oob = _outcome_beyond_bounds(resolution, grid)
    open_count = scaling["open_lower"] + scaling["open_upper"]
    reference = (
        OPEN_BOUND_MASS if oob else (1 - OPEN_BOUND_MASS * open_count) / (len(grid) - 1)
    )
    assert mass is not None
    return (
        mass,
        50 * (log_mass - field_log_score),
        50 * (log_mass - math.log(reference)),
        oob,
    )


def _combine(
    forecasts: Sequence[StoredForecast],
    cdfs: dict[int, list[float]],
    weights: dict[str, float],
) -> list[float]:
    triples = [(f.model_id, cdfs[f.row_id], weights[f.model_id]) for f in forecasts]
    return reconcile_numeric(triples).cdf


def _max_diff(left: Sequence[float], right: Sequence[float]) -> float:
    return max(abs(a - b) for a, b in zip(left, right, strict=True))


def _peer_diff(left: Sequence[float], right: Sequence[float], case: Case) -> float:
    masses = [
        outcome_mass(list(c), case.resolution, case.scaling) for c in (left, right)
    ]
    return 50 * abs(_log_mass(masses[0]) - _log_mass(masses[1]))


def _candidate_sets(case: Case) -> dict[str, list[StoredForecast]]:
    """First and last ok forecast per model: a referee re-run holds two rounds."""
    first: dict[str, StoredForecast] = {}
    last: dict[str, StoredForecast] = {}
    for forecast in case.forecasts:
        first.setdefault(forecast.model_id, forecast)
        last[forecast.model_id] = forecast
    return {"last": list(last.values()), "first": list(first.values())}


def evaluate_case(case: Case) -> CaseResult:
    """Run the reproduction gate and rebuild every variant's combined CDF.

    The forecast set is whichever of "last ok per model" and "first ok per
    model" reproduces the scored submission best. Variants that
    `NumericDistribution` rejects for some model are listed in `failed`.
    """
    parsed: dict[int, list[Percentile] | None] = {}
    per_row: dict[int, dict[str, list[float]]] = {}
    for forecast in case.forecasts:
        try:
            parsed[forecast.row_id] = parse_percentiles(forecast.rationale)
        except ValueError:
            parsed[forecast.row_id] = None
            per_row[forecast.row_id] = {}
            continue
        row_cdfs = {}
        for name, (low, high) in FILTER_VARIANTS.items():
            with contextlib.suppress(ValueError):
                row_cdfs[name] = build_cdf(
                    parsed[forecast.row_id] or [], case.scaling, low, high
                )
        per_row[forecast.row_id] = row_cdfs

    best: tuple[float, str, list[StoredForecast]] | None = None
    for name, forecasts in _candidate_sets(case).items():
        if any(BASE_VARIANT not in per_row[f.row_id] for f in forecasts):
            continue
        base = {f.row_id: per_row[f.row_id][BASE_VARIANT] for f in forecasts}
        diff = _max_diff(_combine(forecasts, base, case.weights), case.submitted)
        if best is None or diff < best[0]:
            best = (diff, name, forecasts)
    if best is None:
        gate = GateRow(
            case.question_id, case.post_id, "none", math.nan, math.nan, math.nan,
            math.nan, "reparse_failed",
        )  # fmt: skip
        return CaseResult(gate, failed=list(VARIANTS))

    _, candidate, forecasts = best
    base = {f.row_id: per_row[f.row_id][BASE_VARIANT] for f in forecasts}
    model_cdf = max(_max_diff(base[f.row_id], f.cdf) for f in forecasts)
    model_peer = max(_peer_diff(base[f.row_id], f.cdf, case) for f in forecasts)
    combined = _combine(forecasts, base, case.weights)
    submitted_cdf = _max_diff(combined, case.submitted)
    submitted_peer = _peer_diff(combined, case.submitted, case)
    reasons = []
    if model_cdf > GATE_MAX_CDF_DIFF or model_peer > GATE_MAX_PEER_DIFF:
        reasons.append("model_cdf_mismatch")
    if submitted_cdf > GATE_MAX_CDF_DIFF or submitted_peer > GATE_MAX_PEER_DIFF:
        reasons.append("submission_mismatch")
    gate = GateRow(
        case.question_id, case.post_id, candidate, model_cdf, model_peer,
        submitted_cdf, submitted_peer, "+".join(reasons),
    )  # fmt: skip

    result = CaseResult(gate)
    for name in FILTER_VARIANTS:
        if all(name in per_row[f.row_id] for f in forecasts):
            result.cdfs[name] = _combine(
                forecasts,
                {f.row_id: per_row[f.row_id][name] for f in forecasts},
                case.weights,
            )
        else:
            result.failed.append(name)
    if BASE_VARIANT in result.cdfs:
        wide = wide_cdf(case.scaling)
        for name, weight in MIX_VARIANTS.items():
            result.cdfs[name] = mix_cdf(result.cdfs[BASE_VARIANT], wide, weight)
    return result


def _post_id(url: str | None) -> str:
    match = _POST_ID_RE.search(url or "")
    return match.group(1) if match else ""


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _latest_run(connection: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        """
        SELECT runs.id, runs.family_id FROM runs
        WHERE runs.question_id = ?
          AND EXISTS (SELECT 1 FROM submissions WHERE run_id = runs.id)
        ORDER BY runs.id DESC LIMIT 1
        """,
        (question_id,),
    ).fetchone()
    return row


def _scored_submission(connection: sqlite3.Connection, run_id: int) -> object:
    """The run's latest `final` submission's forecast, else its latest one."""
    rows = connection.execute(
        "SELECT kind, forecast_json FROM submissions WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    finals = [r for r in rows if r["kind"] == "final"]
    chosen = (finals or rows)[-1]
    return orjson.loads(chosen["forecast_json"])


@dataclass(frozen=True)
class BinaryCase:
    """A resolved binary question with the probability that was scored."""

    question_id: str
    post_id: str
    family: str
    probability: float
    resolution: str
    field_log_score: float


def _resolved_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT q.question_id, q.url, r.resolution, r.field_log_score, r.scaling_json
        FROM questions q JOIN resolutions r ON r.question_id = q.question_id
        WHERE r.status = 'resolved' AND r.field_log_score IS NOT NULL
        ORDER BY q.question_id
        """
    ).fetchall()


def _stored_forecasts(
    connection: sqlite3.Connection, run_id: int
) -> list[StoredForecast]:
    rows = connection.execute(
        """
        SELECT id, model_id, rationale, forecast_json FROM model_forecasts
        WHERE run_id = ? AND status = 'ok' AND forecast_json IS NOT NULL
        ORDER BY id
        """,
        (run_id,),
    ).fetchall()
    return [
        StoredForecast(
            r["id"],
            r["model_id"],
            r["rationale"] or "",
            orjson.loads(r["forecast_json"]),
        )
        for r in rows
    ]


def load_cases(path: Path) -> tuple[list[Case], list[BinaryCase], Counter[str]]:
    """Resolved numeric/discrete cases, resolved binaries, and counted skips.

    Date questions and other types are skipped and counted, never scored.
    A question with several runs uses the latest run that has a submission.
    """
    skipped: Counter[str] = Counter()
    cases: list[Case] = []
    binaries: list[BinaryCase] = []
    with _connect(path) as connection:
        for row in _resolved_rows(connection):
            scaling: Scaling = orjson.loads(row["scaling_json"])
            kind = scaling["type"]
            if kind not in ("numeric", "discrete", "binary"):
                skipped[f"type_{kind}"] += 1
                continue
            run = _latest_run(connection, row["question_id"])
            if run is None:
                skipped["no_submission"] += 1
                continue
            submitted = _scored_submission(connection, run["id"])
            family = run["family_id"] or row["question_id"]
            post_id = _post_id(row["url"])
            if kind == "binary":
                binaries.append(
                    BinaryCase(
                        row["question_id"], post_id, family, float(submitted),  # type: ignore[arg-type]
                        row["resolution"], row["field_log_score"],
                    )
                )  # fmt: skip
                continue
            draws = connection.execute(
                "SELECT model_id, weight FROM draws WHERE run_id = ?", (run["id"],)
            ).fetchall()
            forecasts = _stored_forecasts(connection, run["id"])
            if not forecasts or not isinstance(submitted, list):
                skipped["no_forecast"] += 1
                continue
            cases.append(
                Case(
                    row["question_id"], post_id, family, kind, scaling,
                    row["resolution"], row["field_log_score"], forecasts,
                    {d["model_id"]: d["weight"] for d in draws}, submitted,
                )
            )  # fmt: skip
    return cases, binaries, skipped


def run_numeric(
    cases: Sequence[Case],
) -> tuple[list[GateRow], list[ScoreRow], Counter[str], list[CaseResult]]:
    """Gate and score every case. Questions failing the gate are left out of the rows.

    Returns:
        `(gate_rows, score_rows, variant_failures, results)` where `results`
        is aligned with `cases`.
    """
    gates: list[GateRow] = []
    rows: list[ScoreRow] = []
    failures: Counter[str] = Counter()
    results: list[CaseResult] = []
    for case in cases:
        result = evaluate_case(case)
        results.append(result)
        gates.append(result.gate)
        if not result.gate.passed:
            continue
        failures.update(result.failed)
        for variant, cdf in result.cdfs.items():
            mass, peer, baseline, oob = score_numeric(
                cdf, case.resolution, case.scaling, case.field_log_score
            )
            rows.append(
                ScoreRow(
                    case.question_id, case.post_id, case.family, case.kind,
                    "1" if oob else "0", variant, mass, peer, baseline,
                )
            )  # fmt: skip
    return gates, rows, failures, results


def extremize(probability: float, factor: float) -> float:
    """`expit(factor * logit(p))`, clamped like the bot."""
    logit = math.log(probability / (1 - probability))
    value = 1 / (1 + math.exp(-factor * logit))
    return min(max(value, BINARY_CLAMP[0]), BINARY_CLAMP[1])


def score_binary(binaries: Sequence[BinaryCase]) -> list[ScoreRow]:
    """Peer score (100 * (ln m - F)) of each binary under each extremizing factor."""
    rows = []
    for case in binaries:
        for factor in EXTREMIZE_FACTORS:
            p = extremize(case.probability, factor)
            mass = p if case.resolution == "yes" else 1 - p
            peer = 100 * (math.log(mass) - case.field_log_score)
            rows.append(
                ScoreRow(
                    case.question_id, case.post_id, case.family, "binary", "",
                    f"a{factor:g}", mass, peer, None,
                )
            )  # fmt: skip
    return rows


def bootstrap_resamples(n_families: int, n: int, seed: int) -> list[list[int]]:
    """`n` resamples (with replacement) of family indices, from a fixed seed."""
    rng = random.Random(seed)
    return [rng.choices(range(n_families), k=n_families) for _ in range(n)]


def interval(
    totals: Sequence[float], resamples: Sequence[Sequence[int]]
) -> tuple[float, float]:
    """The central `CI_LEVEL` percentile interval of resampled family-total sums."""
    sums = sorted(sum(totals[i] for i in indices) for indices in resamples)
    tail = (1 - CI_LEVEL) / 2
    return sums[int(tail * len(sums))], sums[int((1 - tail) * len(sums)) - 1]


def _family_totals(
    values: dict[str, float], families: dict[str, str], order: Sequence[str]
) -> list[float]:
    totals: dict[str, float] = defaultdict(float)
    for question_id, value in values.items():
        totals[families[question_id]] += value
    return [totals.get(family, 0.0) for family in order]


def _by_variant(rows: Sequence[ScoreRow]) -> dict[str, dict[str, ScoreRow]]:
    grouped: dict[str, dict[str, ScoreRow]] = defaultdict(dict)
    for row in rows:
        grouped[row.variant][row.question_id] = row
    return grouped


def numeric_tables(
    rows: Sequence[ScoreRow], resamples: Sequence[Sequence[int]], order: Sequence[str]
) -> list[str]:
    """Aggregate and headline tables for the numeric variants, as text lines."""
    grouped = _by_variant(rows)
    families = {r.question_id: r.family for r in rows}
    base = grouped[BASE_VARIANT]
    lines = [
        f"{'variant':<9}{'n':>4}{'peer':>10}{'baseline':>10}{'d_peer':>9}"
        f"{'d_lo90':>9}{'d_hi90':>9}{'oob_n':>6}{'oob_d':>9}{'in_n':>5}{'in_d':>9}",
    ]
    headline = [
        f"{'variant':<9}{'G(oob)':>9}{'L(in)':>9}{'break-even':>11}{'observed':>10}"
    ]
    for variant in VARIANTS:
        scored = grouped.get(variant, {})
        ids = sorted(scored)
        delta = {q: scored[q].peer - base[q].peer for q in ids}
        oob = [q for q in ids if scored[q].oob == "1"]
        inside = [q for q in ids if scored[q].oob == "0"]
        low, high = interval(_family_totals(delta, families, order), resamples)
        lines.append(
            f"{variant:<9}{len(ids):>4}{sum(scored[q].peer for q in ids):>10.1f}"
            f"{sum(scored[q].baseline or 0.0 for q in ids):>10.1f}"
            f"{sum(delta.values()):>9.1f}{low:>9.1f}{high:>9.1f}"
            f"{len(oob):>6}{sum(delta[q] for q in oob):>9.1f}"
            f"{len(inside):>5}{sum(delta[q] for q in inside):>9.1f}"
        )
        if variant == BASE_VARIANT or not ids:
            continue
        gain = sum(delta[q] for q in oob) / len(oob) if oob else math.nan
        loss = -sum(delta[q] for q in inside) / len(inside) if inside else math.nan
        breakeven = loss / (gain + loss) if gain + loss > 0 else math.nan
        observed = len(oob) / len(ids)
        headline.append(
            f"{variant:<9}{gain:>9.2f}{loss:>9.2f}{breakeven:>11.4f}{observed:>10.4f}"
        )
    return [*lines, "", *headline]


def binary_tables(
    binaries: Sequence[BinaryCase],
    rows: Sequence[ScoreRow],
    resamples: Sequence[Sequence[int]],
    order: Sequence[str],
) -> list[str]:
    """Calibration table and extremizing totals for the binaries, as text lines."""
    probabilities = [b.probability for b in binaries]
    yes = [b.resolution == "yes" for b in binaries]
    lines = [
        f"n={len(binaries)}  "
        f"p range {min(probabilities):.3f}-{max(probabilities):.3f}  "
        f"mean p {sum(probabilities) / len(binaries):.3f}  "
        f"yes-rate {sum(yes) / len(binaries):.3f}",
        "",
        f"{'p bin':<12}{'n':>4}{'mean p':>9}{'yes-rate':>10}",
    ]
    for low, high in itertools.pairwise(CALIBRATION_EDGES):
        members = [
            (b.probability, y)
            for b, y in zip(binaries, yes, strict=True)
            if low <= b.probability < high or (high == 1.0 and b.probability == 1.0)
        ]
        if members:
            lines.append(
                f"{low:.1f}-{high:.1f}".ljust(12)
                + f"{len(members):>4}{sum(p for p, _ in members) / len(members):>9.3f}"
                f"{sum(y for _, y in members) / len(members):>10.3f}"
            )
    grouped = _by_variant(rows)
    families = {r.question_id: r.family for r in rows}
    base = grouped["a1"]
    lines += [
        "",
        f"{'variant':<9}{'peer':>9}{'lo90':>9}{'hi90':>9}{'d_vs_a1':>9}{'d_lo90':>9}{'d_hi90':>9}",
    ]
    for variant, scored in grouped.items():
        peer = {q: r.peer for q, r in scored.items()}
        delta = {q: scored[q].peer - base[q].peer for q in scored}
        t_low, t_high = interval(_family_totals(peer, families, order), resamples)
        d_low, d_high = interval(_family_totals(delta, families, order), resamples)
        lines.append(
            f"{variant:<9}{sum(peer.values()):>9.1f}{t_low:>9.1f}{t_high:>9.1f}"
            f"{sum(delta.values()):>9.1f}{d_low:>9.1f}{d_high:>9.1f}"
        )
    return lines


def gate_lines(
    gates: Sequence[GateRow],
    check_posts: Sequence[str],
    rows: Sequence[ScoreRow],
    skipped: Counter[str],
) -> list[str]:
    """The reproduction gate's summary, worst mismatches and the -106.5 check."""
    failed = [g for g in gates if not g.passed]
    candidates = Counter(g.candidate for g in gates)
    lines = [
        f"questions {len(gates)}  passed {len(gates) - len(failed)}  "
        f"failed {len(failed)}  skipped {dict(skipped)}  "
        f"forecast set {dict(candidates)}",
        f"tolerance: max abs CDF diff {GATE_MAX_CDF_DIFF}, "
        f"peer diff {GATE_MAX_PEER_DIFF}",
    ]
    worst = sorted(
        (g for g in gates if g.candidate != "none"),
        key=lambda g: max(g.model_peer_diff, g.submitted_peer_diff),
        reverse=True,
    )[:5]
    lines.append(
        f"{'question':<18}{'set':>6}{'model_cdf':>11}{'model_peer':>11}"
        f"{'sub_cdf':>10}{'sub_peer':>10}  reason"
    )
    for g in worst:
        lines.append(
            f"{g.question_id:<18}{g.candidate:>6}{g.model_cdf_diff:>11.2e}"
            f"{g.model_peer_diff:>11.3f}{g.submitted_cdf_diff:>10.2e}"
            f"{g.submitted_peer_diff:>10.3f}  {g.reason or 'ok'}"
        )
    lines += [f"EXCLUDED {g.question_id} post {g.post_id}: {g.reason}" for g in failed]
    base_by_post = {r.post_id: r for r in rows if r.variant == BASE_VARIANT}
    for post in check_posts:
        row = base_by_post.get(post)
        if row is None:
            lines.append(f"CHECK post {post}: not scored (excluded or absent)")
            continue
        good = abs((row.baseline or math.nan) - CHECK_BASELINE) <= CHECK_TOLERANCE
        lines.append(
            f"CHECK post {post}: p1_p99 baseline {row.baseline:.2f} "
            f"(expect {CHECK_BASELINE:.2f} +/- {CHECK_TOLERANCE}) "
            f"{'PASS' if good else 'FAIL'}"
        )
    return lines


def _write_tsv(
    path: Path, rows: Sequence[Sequence[object]], header: Sequence[str]
) -> None:
    body = ["\t".join(header)]
    body += ["\t".join("" if v is None else str(v) for v in row) for row in rows]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _score_tsv(rows: Sequence[ScoreRow]) -> list[list[object]]:
    return [
        [
            r.question_id,
            r.post_id,
            r.family,
            r.kind,
            r.oob,
            r.variant,
            r.mass,
            r.peer,
            r.baseline,
        ]
        for r in sorted(rows, key=lambda r: (r.question_id, r.variant))
    ]


def run_backtest(
    ledger: Path, out: Path, seed: int, resamples: int, check_posts: Sequence[str]
) -> str:
    """Run the whole backtest, write the TSVs and aggregates, return the aggregates."""
    out.mkdir(parents=True, exist_ok=True)
    cases, binaries, skipped = load_cases(ledger)
    gates, rows, failures, _ = run_numeric(cases)
    header = [
        "question_id",
        "post_id",
        "family",
        "kind",
        "oob",
        "variant",
        "mass",
        "peer",
        "baseline",
    ]
    _write_tsv(out / "tail-backtest-questions.tsv", _score_tsv(rows), header)
    binary_rows = score_binary(binaries)
    _write_tsv(out / "tail-backtest-binary.tsv", _score_tsv(binary_rows), header)
    _write_tsv(
        out / "tail-backtest-gate.tsv",
        [
            [g.question_id, g.post_id, g.candidate, g.model_cdf_diff, g.model_peer_diff,
             g.submitted_cdf_diff, g.submitted_peer_diff, g.reason or "ok"]
            for g in gates
        ],
        ["question_id", "post_id", "set", "model_cdf_diff", "model_peer_diff",
         "submitted_cdf_diff", "submitted_peer_diff", "result"],
    )  # fmt: skip

    numeric_order = sorted({c.family for c in cases})
    binary_order = sorted({b.family for b in binaries})
    numeric_resamples = bootstrap_resamples(len(numeric_order), resamples, seed)
    binary_resamples = bootstrap_resamples(len(binary_order), resamples, seed)
    sections = [
        f"bootstrap: cluster by runs.family_id, {resamples} resamples, seed {seed}, "
        f"{int(CI_LEVEL * 100)}% percentile interval",
        "", "== A0. reproduction gate ==",
        *gate_lines(gates, check_posts, rows, skipped),
        "", "== A1. numeric/discrete, peer and baseline points (delta vs p1_p99) ==",
        *numeric_tables(rows, numeric_resamples, numeric_order),
        "", "variant failures (question-variants NumericDistribution rejected): "
        f"{dict(failures)}",
        "", "== B. binary (peer points, 100 * (ln m - F)) ==",
        *binary_tables(binaries, binary_rows, binary_resamples, binary_order),
    ]  # fmt: skip
    text = "\n".join(sections) + "\n"
    (out / "tail-backtest-aggregates.txt").write_text(text, encoding="utf-8")
    return text


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry: `python -m betomcat.backtest --ledger PATH --out DIR`."""
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0] if __doc__ else ""
    )
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--check-posts", default="", help="comma-separated post ids")
    args = parser.parse_args(argv)
    posts = [p for p in args.check_posts.split(",") if p]
    print(run_backtest(args.ledger, args.out, args.seed, args.resamples, posts))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
