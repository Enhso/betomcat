"""Tests for `cli.build_deps`: two-key OpenRouter routing, budget guard wiring."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from betomcat.budget import BudgetGuard
from betomcat.cli import build_deps
from betomcat.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        metaculus_token="meta-token",
        openrouter_api_key="funded-key",
        openrouter_free_api_key="free-key",
        asknews_api_key=None,
        iw_url="http://127.0.0.1:8080",
        data_dir=tmp_path,
        soft_threshold_min=30,
        hard_threshold_min=5,
        poll_seconds=300,
        dry_run=True,
        tournaments=(1,),
        budget_window_end=datetime(2026, 10, 5, tzinfo=UTC),
    )


async def test_build_deps_wires_both_openrouter_keys_into_the_llm_client(
    tmp_path: Path,
) -> None:
    deps = build_deps(_settings(tmp_path), dry_run=True)
    try:
        assert deps.llm._api_key == "funded-key"
        assert deps.llm._free_api_key == "free-key"
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
