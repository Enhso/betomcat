"""Runtime settings for betomcat, loaded from the environment.

Secrets (tokens/keys) are read but never logged. Call :func:`load_settings`
once at process start; downstream modules take a `Settings` instance rather
than reading `os.environ` themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from forecasting_tools.helpers.metaculus_client import MetaculusClient

# End of MiniBench round 2 (5-19 Oct; Hatim, 2026-09-25). Past this date the
# guard allows the whole remaining credit in a single day, so move it before.
DEFAULT_BUDGET_WINDOW_END = datetime(2026, 10, 19, tzinfo=UTC)


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean environment variable (`1/true/yes/on`, case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _datetime_env(name: str, default: datetime) -> datetime:
    """Parse an ISO 8601 UTC timestamp env var (`Z` suffix accepted)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))


@dataclass(frozen=True)
class Settings:
    """betomcat runtime configuration.

    Attributes:
        metaculus_token: Metaculus API token for the v1 bot account.
        openrouter_api_key: OpenRouter key funding all forecasting LLM calls.
        openrouter_free_api_key: Personal OpenRouter key (50 free req/day)
            used for `:free` pool models that the funded key's
            allowed-providers list refuses (BUILD_LOG 2026-09-22 evening).
        asknews_api_key: AskNews key used for direct research (degraded mode).
        iw_url: Base URL of the Intelligence Workbench HTTP API.
        data_dir: Root directory for the ledger, weights file, and outbox.
        soft_threshold_min: Minutes before question close to start retries.
        hard_threshold_min: Minutes before question close to stop everything.
        poll_seconds: Daemon poll interval for new tournament questions.
        dry_run: If true, submissions and comments are logged, not sent.
        tournaments: Tournament ids/slugs the daemon polls.
        binary_clamp: (min, max) clamp applied to the reconciled binary
            probability before submission.
        budget_window_end: End of the budget-pacing window (spec s5's
            "two MiniBench weeks"); the daily budget is paced against this.
    """

    metaculus_token: str | None
    openrouter_api_key: str | None
    openrouter_free_api_key: str | None
    asknews_api_key: str | None
    iw_url: str
    data_dir: Path
    soft_threshold_min: int
    hard_threshold_min: int
    poll_seconds: int
    dry_run: bool
    tournaments: tuple[int | str, ...] = field(
        default_factory=lambda: (
            MetaculusClient.CURRENT_AI_COMPETITION_ID,
            MetaculusClient.CURRENT_MINIBENCH_ID,
        )
    )
    binary_clamp: tuple[float, float] = (0.01, 0.99)
    budget_window_end: datetime = field(
        default_factory=lambda: DEFAULT_BUDGET_WINDOW_END
    )


def load_settings(env_path: Path | str | None = None) -> Settings:
    """Load settings from `.env` (if present) and the process environment.

    Args:
        env_path: Optional explicit path to a `.env` file. Defaults to
            python-dotenv's normal discovery (nearest `.env` upward from cwd).

    Returns:
        A populated, immutable `Settings` instance.
    """
    load_dotenv(dotenv_path=env_path)
    return Settings(
        metaculus_token=os.environ.get("METACULUS_TOKEN") or None,
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        openrouter_free_api_key=os.environ.get("OPENROUTER_FREE_API_KEY") or None,
        asknews_api_key=os.environ.get("ASKNEWS_API_KEY") or None,
        iw_url=os.environ.get("IW_URL", "http://127.0.0.1:8080"),
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        soft_threshold_min=_int_env("SOFT_THRESHOLD_MIN", 30),
        hard_threshold_min=_int_env("HARD_THRESHOLD_MIN", 5),
        poll_seconds=_int_env("POLL_SECONDS", 300),
        dry_run=_bool_env("DRY_RUN", False),
        budget_window_end=_datetime_env("BUDGET_WINDOW_END", DEFAULT_BUDGET_WINDOW_END),
    )
