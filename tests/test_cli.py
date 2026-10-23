"""Tests for `cli.build_deps`: LLM key routing, budget guard wiring."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat import weights as weights_module
from betomcat.budget import BudgetGuard
from betomcat.cli import build_deps, main
from betomcat.config import Settings
from betomcat.ledger import Ledger
from betomcat.pool import load_weights


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        metaculus_token="meta-token",
        openrouter_api_key="funded-key",
        openrouter_free_api_key="free-key",
        gemini_api_key="gemini-key",
        asknews_api_key=None,
        iw_url="http://127.0.0.1:8080",
        data_dir=tmp_path,
        soft_threshold_min=30,
        hard_threshold_min=5,
        poll_seconds=300,
        dry_run=True,
        tournaments=(1,),
        budget_window_end=datetime(2026, 10, 5, tzinfo=UTC),
        minibench_round_budget_usd=42.0,
        minibench_busy_days=3,
    )


async def test_build_deps_wires_all_llm_keys_into_the_llm_client(
    tmp_path: Path,
) -> None:
    deps = build_deps(_settings(tmp_path), dry_run=True)
    try:
        assert deps.llm._api_key == "funded-key"
        assert deps.llm._free_api_key == "free-key"
        assert deps.llm._gemini_api_key == "gemini-key"
    finally:
        await deps.iw.aclose()
        await deps.llm.aclose()
        deps.ledger.close()


async def test_build_deps_configures_a_live_budget_guard(tmp_path: Path) -> None:
    deps = build_deps(_settings(tmp_path), dry_run=True)
    try:
        assert isinstance(deps.budget, BudgetGuard)
        assert deps.budget.funded_api_key == "funded-key"
        assert deps.budget.free_api_key == "free-key"
        assert deps.budget.window_end == datetime(2026, 10, 5, tzinfo=UTC)
        assert deps.budget.metaculus_token == "meta-token"
        assert deps.budget.round_budget_usd == 42.0
        assert deps.budget.minibench_busy_days == 3
    finally:
        await deps.iw.aclose()
        await deps.llm.aclose()
        deps.ledger.close()


async def test_build_deps_budget_guard_falls_open_with_no_keys(tmp_path: Path) -> None:
    """A missing free key must not break the guard: it just fails open for it."""
    settings = dataclasses.replace(
        _settings(tmp_path), openrouter_api_key=None, openrouter_free_api_key=None
    )
    deps = build_deps(settings, dry_run=True)
    try:
        assert deps.budget.funded_api_key is None
        assert deps.budget.free_api_key is None
    finally:
        await deps.iw.aclose()
        await deps.llm.aclose()
        deps.ledger.close()


async def test_build_deps_leaves_the_gemini_key_unset_when_not_configured(
    tmp_path: Path,
) -> None:
    settings = dataclasses.replace(_settings(tmp_path), gemini_api_key=None)
    deps = build_deps(settings, dry_run=True)
    try:
        assert deps.llm._gemini_api_key is None
    finally:
        await deps.iw.aclose()
        await deps.llm.aclose()
        deps.ledger.close()


def test_update_weights_command_scores_the_ledger_and_writes_weights_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    data_dir = tmp_path / "data"
    pool = tmp_path / "models.yaml"
    pool.write_text(
        "models:\n"
        "  - {id: model-a, tier: frontier, enabled: true}\n"
        "  - {id: model-b, tier: frontier, enabled: true}\n"
    )
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("METACULUS_TOKEN", "cli-token")
    monkeypatch.setattr("betomcat.cli.DEFAULT_POOL_PATH", pool)
    monkeypatch.setattr(weights_module, "REQUEST_PAUSE_SECONDS", 0.0)

    post = orjson.loads(
        (
            Path(__file__).parent / "fixtures" / "metaculus_post_45948_binary.json"
        ).read_bytes()
    )
    with Ledger(data_dir / "ledger.sqlite") as ledger:
        ledger.upsert_question(
            "metaculus:46130",
            post["title"],
            "binary",
            "2026-10-07T19:46:43+00:00",
            "https://www.metaculus.com/questions/45948",
        )
        run = ledger.start_run("metaculus:46130")
        for model_id, forecast in (("model-a", 0.2), ("model-b", 0.8)):
            ledger.record_model_forecast(
                run.id,
                model_id,
                1,
                "t",
                "t",
                "ok",
                forecast,
                None,
                None,
                None,
                None,
                None,
            )
    httpx_mock.add_response(url="https://www.metaculus.com/api/posts/45948/", json=post)

    assert main(["update-weights"]) == 0

    with Ledger(data_dir / "ledger.sqlite") as ledger:
        rows = ledger.get_model_scores()
    scores = {r["model_id"]: r["score"] for r in rows}
    methods = {r["method"] for r in rows}
    # Resolved "no". The bot gave P(no) 0.815 and Metaculus gave it a spot peer
    # score of -3.296704306198748, so the field's mean log score is
    # F = ln(0.815) + 0.03296704306198748. model-a gave P(no) 0.8, model-b 0.2.
    # By hand: ln(0.8) - F and ln(0.2) - F.
    assert scores == {
        "model-a": pytest.approx(-0.05154342863492278),
        "model-b": pytest.approx(-1.4378377897548134),
    }
    assert methods == {"peer"}
    written = orjson.loads((data_dir / "weights.json").read_bytes())
    assert written["n_resolved"] == {"model-a": 1, "model-b": 1}
    assert load_weights(data_dir) == {"model-a": 1.0, "model-b": 1.0}  # below 10
    assert written["updated_at"][:10] == datetime.now(UTC).date().isoformat()
