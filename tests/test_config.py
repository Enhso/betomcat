"""Tests for Settings loading from the environment."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from betomcat.config import load_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "METACULUS_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_FREE_API_KEY",
        "GEMINI_API_KEY",
        "ASKNEWS_API_KEY",
        "IW_URL",
        "DATA_DIR",
        "SOFT_THRESHOLD_MIN",
        "HARD_THRESHOLD_MIN",
        "POLL_SECONDS",
        "DRY_RUN",
        "BUDGET_WINDOW_END",
        "MINIBENCH_ROUND_BUDGET_USD",
        "MINIBENCH_BUSY_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_load_settings_defaults(tmp_path: Path) -> None:
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.metaculus_token is None
    assert settings.openrouter_api_key is None
    assert settings.openrouter_free_api_key is None
    assert settings.gemini_api_key is None
    assert settings.budget_window_end == datetime(2026, 10, 19, tzinfo=UTC)
    assert settings.minibench_round_budget_usd == pytest.approx(50.0)
    assert settings.minibench_busy_days == 4
    assert settings.iw_url == "http://127.0.0.1:8080"
    assert settings.data_dir == Path("./data")
    assert settings.soft_threshold_min == 30
    assert settings.hard_threshold_min == 5
    assert settings.poll_seconds == 300
    assert settings.dry_run is False
    assert settings.binary_clamp == (0.01, 0.99)
    assert len(settings.tournaments) == 2


def test_load_settings_reads_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", "secret-token")
    monkeypatch.setenv("DATA_DIR", "/tmp/betomcat-data")
    monkeypatch.setenv("SOFT_THRESHOLD_MIN", "15")
    monkeypatch.setenv("HARD_THRESHOLD_MIN", "2")
    monkeypatch.setenv("POLL_SECONDS", "60")
    monkeypatch.setenv("DRY_RUN", "true")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.metaculus_token == "secret-token"
    assert settings.data_dir == Path("/tmp/betomcat-data")
    assert settings.soft_threshold_min == 15
    assert settings.hard_threshold_min == 2
    assert settings.poll_seconds == 60
    assert settings.dry_run is True


def test_load_settings_dry_run_false_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRY_RUN", "0")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.dry_run is False


def test_load_settings_reads_free_key_and_window_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_FREE_API_KEY", "free-secret")
    monkeypatch.setenv("BUDGET_WINDOW_END", "2026-11-01T00:00:00Z")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.openrouter_free_api_key == "free-secret"
    assert settings.budget_window_end == datetime(2026, 11, 1, tzinfo=UTC)


def test_load_settings_reads_minibench_round_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIBENCH_ROUND_BUDGET_USD", "42.5")
    monkeypatch.setenv("MINIBENCH_BUSY_DAYS", "3")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.minibench_round_budget_usd == pytest.approx(42.5)
    assert settings.minibench_busy_days == 3


def test_load_settings_blank_minibench_env_vars_use_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIBENCH_ROUND_BUDGET_USD", "  ")
    monkeypatch.setenv("MINIBENCH_BUSY_DAYS", "")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.minibench_round_budget_usd == pytest.approx(50.0)
    assert settings.minibench_busy_days == 4


def test_load_settings_reads_gemini_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.gemini_api_key == "gemini-secret"


def test_load_settings_blank_gemini_api_key_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")

    settings = load_settings(empty_env)

    assert settings.gemini_api_key is None
