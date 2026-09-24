"""Model pool config (config/models.yaml) and the weighted draw (spec s5).

The pool file is re-read on every draw so it stays hot-editable without a
redeploy. Weights come from `DATA_DIR/weights.json`, written by the daily
weight-update job (a later chunk); a model missing from that file gets
weight 1.0, so an empty/missing weights file degrades to uniform-random
selection, as required by spec s5's graceful-degradation clause.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import orjson
import yaml

logger = logging.getLogger(__name__)

KeyChoice = Literal["funded", "free"]


@dataclass(frozen=True)
class ModelSpec:
    """One model pool entry, as read from `config/models.yaml`."""

    id: str
    tier: str
    enabled: bool
    max_tokens: int | None = None
    reasoning: dict[str, Any] | None = None
    notes: str | None = None
    key: KeyChoice | None = None
    price_in: float | None = None
    price_out: float | None = None
    context_tokens: int | None = None


def effective_key(model: ModelSpec) -> KeyChoice:
    """Which OpenRouter key (funded or personal free-tier) serves this model.

    An explicit `key:` in the pool YAML wins. Otherwise: `:free` ids route to
    the free key, except `google/`-prefixed ones -- the funded key's
    allowed-providers list includes google-ai-studio, so Google `:free`
    models work there too and stay on the higher-quota funded key
    (BUILD_LOG 2026-09-22 evening).
    """
    if model.key is not None:
        return model.key
    if model.id.endswith(":free") and not model.id.startswith("google/"):
        return "free"
    return "funded"


@dataclass(frozen=True)
class PoolConfig:
    """The full model pool: ensemble width plus every configured model."""

    ensemble_width: int
    models: list[ModelSpec]

    @property
    def enabled_models(self) -> list[ModelSpec]:
        return [m for m in self.models if m.enabled]


@dataclass(frozen=True)
class DrawResult:
    """One draw's outcome, frozen at draw time (spec s5's weight-freeze rule).

    `weights` holds only the drawn models' weights, exactly as read at the
    moment of the draw -- callers must reuse these values for reconciliation
    rather than re-reading `weights.json` later.
    """

    models: list[str]
    weights: dict[str, float]
    pool_avg: float
    fallback_fired: bool


def _parse_key(raw_key: Any, model_id: str) -> KeyChoice | None:
    if raw_key is None:
        return None
    if raw_key not in ("funded", "free"):
        raise ValueError(
            f"model {model_id}: key must be 'funded' or 'free', got {raw_key!r}"
        )
    return cast(KeyChoice, raw_key)


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def load_pool(path: Path | str) -> PoolConfig:
    """Parse `config/models.yaml` into a `PoolConfig`.

    Args:
        path: Path to the YAML pool file.

    Returns:
        The parsed pool config.
    """
    with open(path, "rb") as f:
        raw = yaml.safe_load(f)
    models = [
        ModelSpec(
            id=entry["id"],
            tier=entry["tier"],
            enabled=bool(entry["enabled"]),
            max_tokens=entry.get("max_tokens"),
            reasoning=entry.get("reasoning"),
            notes=entry.get("notes"),
            key=_parse_key(entry.get("key"), entry["id"]),
            price_in=_float_or_none(entry.get("price_in")),
            price_out=_float_or_none(entry.get("price_out")),
            context_tokens=entry.get("context_tokens"),
        )
        for entry in raw.get("models", [])
    ]
    return PoolConfig(ensemble_width=int(raw.get("ensemble_width", 2)), models=models)


def load_weights(data_dir: Path | str) -> dict[str, float]:
    """Read `DATA_DIR/weights.json`. Missing or empty -> `{}` (uniform).

    Args:
        data_dir: The bot's data directory.

    Returns:
        A mapping of model id to weight. Absent entries default to 1.0
        wherever a weight is looked up (see `effective_weight`).
    """
    path = Path(data_dir) / "weights.json"
    if not path.exists():
        return {}
    try:
        data = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError:
        logger.warning("weights.json at %s is not valid JSON; using uniform", path)
        return {}
    weights = data.get("weights", {})
    return {str(k): float(v) for k, v in weights.items()}


def effective_weight(model_id: str, weights: dict[str, float]) -> float:
    """A model's weight, defaulting to 1.0 when absent from `weights`."""
    return weights.get(model_id, 1.0)


def _weighted_choice(
    rng: random.Random, candidates: list[ModelSpec], weights: dict[str, float]
) -> ModelSpec:
    """Pick one candidate, probability proportional to its effective weight."""
    raw_weights = [max(effective_weight(m.id, weights), 0.0) for m in candidates]
    total = sum(raw_weights)
    if total <= 0:
        return rng.choice(candidates)
    target = rng.random() * total
    cumulative = 0.0
    for model, w in zip(candidates, raw_weights, strict=True):
        cumulative += w
        if cumulative >= target:
            return model
    return candidates[-1]


def draw_replacement(
    rng: random.Random, candidates: list[ModelSpec], weights: dict[str, float]
) -> ModelSpec:
    """Pick a mid-run replacement model (public wrapper over `_weighted_choice`).

    Weighted-random over `candidates`, exactly like step 1 of `draw` -- no
    pool-average constraint applies, since this isn't picking an ensemble,
    just substituting one slot whose drawn model gave up.

    Args:
        rng: Injected RNG, for deterministic tests.
        candidates: Untried, budget-eligible models available as spares.
        weights: Model weights to draw against (the frozen snapshot from the
            original draw -- never re-read live).

    Returns:
        The chosen replacement model.

    Raises:
        ValueError: If `candidates` is empty.
    """
    if not candidates:
        raise ValueError("no replacement candidates available")
    return _weighted_choice(rng, candidates, weights)


def draw(
    pool: PoolConfig,
    weights: dict[str, float],
    rng: random.Random,
    width: int | None = None,
) -> DrawResult:
    """Draw `width` distinct models from the enabled pool (spec s5 steps 1-4).

    Step 1: the first model is an unconstrained weighted-random draw over the
    full enabled pool. Step 2+: each subsequent model is drawn from the
    subset of remaining enabled models whose weight is high enough that the
    chosen set's average weight would be >= the pool's overall average
    (`weight >= (k+1)*pool_avg - sum(weights already chosen)`, `k` = number
    already chosen). Step 3: if that subset is empty, fall back to the
    highest-weighted remaining model and flag `fallback_fired`.

    Args:
        pool: The model pool (already filtered to `enabled_models` here).
        weights: Current model weights (`load_weights` output).
        rng: Injected RNG, for deterministic tests.
        width: Ensemble width. Defaults to `pool.ensemble_width`.

    Returns:
        The drawn model ids with their frozen weights, the pool average, and
        whether the degenerate fallback fired for any pick after the first.

    Raises:
        ValueError: If fewer enabled models exist than `width`.
    """
    enabled = pool.enabled_models
    width = pool.ensemble_width if width is None else width
    if len(enabled) < width:
        raise ValueError(
            f"Need {width} enabled models to draw an ensemble, only "
            f"{len(enabled)} enabled"
        )

    pool_avg = sum(effective_weight(m.id, weights) for m in enabled) / len(enabled)

    remaining = list(enabled)
    chosen: list[ModelSpec] = []
    chosen_weights: list[float] = []
    fallback_fired = False

    for k in range(width):
        if k == 0:
            candidates = remaining
        else:
            threshold = (k + 1) * pool_avg - sum(chosen_weights)
            candidates = [
                m for m in remaining if effective_weight(m.id, weights) >= threshold
            ]
            if not candidates:
                fallback_fired = True
                best = max(remaining, key=lambda m: effective_weight(m.id, weights))
                candidates = [best]
                logger.warning(
                    "draw fallback fired: no model >= threshold %.4f after "
                    "choosing %s; falling back to highest-weighted %s",
                    threshold,
                    [m.id for m in chosen],
                    best.id,
                )
        pick = _weighted_choice(rng, candidates, weights)
        chosen.append(pick)
        chosen_weights.append(effective_weight(pick.id, weights))
        remaining.remove(pick)

    return DrawResult(
        models=[m.id for m in chosen],
        weights={m.id: w for m, w in zip(chosen, chosen_weights, strict=True)},
        pool_avg=pool_avg,
        fallback_fired=fallback_fired,
    )


def write_weights(
    data_dir: Path | str,
    weights: dict[str, float],
    n_resolved: dict[str, int],
    updated_at: str,
) -> None:
    """Write `DATA_DIR/weights.json` (used by tests and the weight job).

    Args:
        data_dir: The bot's data directory.
        weights: Model id -> weight.
        n_resolved: Model id -> number of resolved questions behind the weight.
        updated_at: ISO 8601 UTC timestamp of this write.
    """
    path = Path(data_dir) / "weights.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": updated_at, "weights": weights, "n_resolved": n_resolved}
    path.write_bytes(orjson.dumps(payload))
