"""Tests for the model pool loader and the weighted draw (spec s5)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from betomcat.pool import (
    ModelSpec,
    PoolConfig,
    draw,
    load_pool,
    load_weights,
    write_weights,
)

MODELS_YAML = """
ensemble_width: 2
models:
  - id: model-a
    tier: frontier
    enabled: true
    max_tokens: 4000
    reasoning:
      effort: medium
    notes: null
  - id: model-b
    tier: frontier
    enabled: true
  - id: model-c
    tier: free
    enabled: true
  - id: model-disabled
    tier: free
    enabled: false
"""


def _pool(*specs: ModelSpec, width: int = 2) -> PoolConfig:
    return PoolConfig(ensemble_width=width, models=list(specs))


def test_load_pool_parses_yaml(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(MODELS_YAML)

    pool = load_pool(path)

    assert pool.ensemble_width == 2
    assert [m.id for m in pool.models] == [
        "model-a",
        "model-b",
        "model-c",
        "model-disabled",
    ]
    assert pool.models[0].reasoning == {"effort": "medium"}
    assert pool.models[0].max_tokens == 4000
    assert [m.id for m in pool.enabled_models] == ["model-a", "model-b", "model-c"]


def test_load_weights_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_weights(tmp_path) == {}


def test_load_weights_roundtrip(tmp_path: Path) -> None:
    write_weights(
        tmp_path,
        weights={"model-a": 2.5, "model-b": 0.5},
        n_resolved={"model-a": 10, "model-b": 3},
        updated_at="2026-09-22T00:00:00Z",
    )

    weights = load_weights(tmp_path)

    assert weights == {"model-a": 2.5, "model-b": 0.5}


def test_load_weights_corrupt_file_degrades_to_uniform(tmp_path: Path) -> None:
    (tmp_path / "weights.json").write_bytes(b"not json")

    assert load_weights(tmp_path) == {}


def test_draw_uniform_degradation_picks_from_full_pool() -> None:
    pool = _pool(
        ModelSpec("model-a", "frontier", True),
        ModelSpec("model-b", "frontier", True),
        ModelSpec("model-c", "free", True),
    )
    rng = random.Random(0)

    result = draw(pool, weights={}, rng=rng)

    assert len(result.models) == 2
    assert len(set(result.models)) == 2
    assert all(w == 1.0 for w in result.weights.values())
    assert result.pool_avg == pytest.approx(1.0)
    assert result.fallback_fired is False


def test_draw_freezes_weights_at_draw_time() -> None:
    pool = _pool(
        ModelSpec("model-a", "frontier", True),
        ModelSpec("model-b", "frontier", True),
    )
    weights = {"model-a": 3.0, "model-b": 1.0}
    rng = random.Random(1)

    result = draw(pool, weights, rng)

    assert set(result.models) == {"model-a", "model-b"}
    assert result.weights == {"model-a": 3.0, "model-b": 1.0}
    # Mutating the source dict afterward must not affect the frozen draw.
    weights["model-a"] = 999.0
    assert result.weights["model-a"] == 3.0


def test_draw_second_pick_respects_constraint_subset() -> None:
    # pool_avg = (10 + 1 + 1) / 3 = 4. If model-a (weight 10) is drawn first,
    # the constraint for the second pick is weight >= 2*4 - 10 = -2, so every
    # remaining model qualifies -- the subset is unconstrained in practice.
    # Use a case where the constraint actually excludes a model instead:
    # pool_avg = (10 + 1 + 1) / 3 = 4; if model-b (weight 1) drawn first,
    # threshold = 2*4 - 1 = 7, so only model-a (weight 10) qualifies.
    pool = _pool(
        ModelSpec("model-a", "frontier", True),
        ModelSpec("model-b", "frontier", True),
        ModelSpec("model-c", "free", True),
    )
    weights = {"model-a": 10.0, "model-b": 1.0, "model-c": 1.0}

    # Force model-b to be drawn first by stubbing the RNG's first draw via a
    # rigged random: rng.random() returns a value that lands on model-b in
    # the cumulative-weight walk. Easiest: run many seeds and assert the
    # invariant holds whenever model-b (or model-c) is drawn first.
    for seed in range(200):
        rng = random.Random(seed)
        result = draw(pool, weights, rng)
        first, second = result.models
        if first in ("model-b", "model-c"):
            # threshold = 2*4 - 1 = 7; only model-a (10.0) clears it.
            assert second == "model-a"


def test_draw_degenerate_fallback_fires_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # weights {a: 5, b: 4, c: 1}, pool_avg = 10/3 = 3.3333. If the smallest
    # model (c, weight 1) is drawn first, the second-pick threshold is
    # 2*3.3333 - 1 = 5.6667, which exceeds both remaining weights (5 and 4)
    # -> the constraint subset is empty and the fallback must fire, picking
    # the highest-weighted remaining model (a, weight 5).
    pool = _pool(
        ModelSpec("model-a", "frontier", True),
        ModelSpec("model-b", "frontier", True),
        ModelSpec("model-c", "free", True),
    )
    weights = {"model-a": 5.0, "model-b": 4.0, "model-c": 1.0}

    caplog.set_level("WARNING")
    found_c_first = False
    for seed in range(500):
        rng = random.Random(seed)
        caplog.clear()
        result = draw(pool, weights, rng)
        if result.models[0] == "model-c":
            found_c_first = True
            assert result.fallback_fired is True
            assert result.models[1] == "model-a"
            assert any("fallback fired" in message for message in caplog.messages)
        else:
            assert result.fallback_fired is False
    assert found_c_first, "expected at least one seed to draw model-c first"


def test_draw_raises_when_pool_too_small() -> None:
    pool = _pool(ModelSpec("model-a", "frontier", True), width=2)

    with pytest.raises(ValueError, match="Need 2 enabled models"):
        draw(pool, weights={}, rng=random.Random(0))
