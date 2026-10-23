"""`betomcat summary`: the weekly state summary, mailed or written to a file.

Sources: the newest state snapshot (ledger and weights, restored into a temp
dir that is deleted afterwards), the Metaculus API (both bots), OpenRouter
(credit) and GitHub (pool commits, open PRs, workflow runs). A source that
fails turns its own cells or section into "unavailable"; the rest still
sends. The repo and its Actions logs are public, so only counts are logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson

from betomcat.budget import KeyStatus
from betomcat.config import Settings
from betomcat.ledger import Ledger
from betomcat.mail import mail_credentials, send_email
from betomcat.pipeline import asknews_cycle_start
from betomcat.report import UNAVAILABLE, Section, render_html, render_text, unavailable
from betomcat.sources import (
    BETOMCAT_REPO,
    FE_FALL_PROJECT_ID,
    VEZO3,
    VEZOCONTROL,
    Bot,
    GitHubReader,
    MetaculusReader,
    attempt,
    find_standing,
    last_forecast_time,
    round_is_finalized,
)
from betomcat.state import GitHubReleases, restore_latest
from betomcat.watch import WATCH_TAG, Alert, evaluate, load_state, observe

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(days=7)
POOL_PATH = "config/models.yaml"
NOT_LISTED = "not listed"
BOTS = (VEZO3, VEZOCONTROL)


@dataclass(frozen=True)
class LedgerReport:
    """vezo3's last seven days, read from the restored snapshot."""

    runs_by_status: dict[str, int]
    degraded_runs: int
    models: list[dict[str, Any]]
    asknews_used: int
    asknews_cap: int
    cycle_start: datetime
    weights: list[tuple[str, float, int]] | None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _read_weights(path: Path) -> list[tuple[str, float, int]] | None:
    """(model, weight, n_resolved) from `weights.json`, heaviest first."""
    try:
        data = orjson.loads(path.read_bytes())
        resolved = data.get("n_resolved", {})
        rows = [
            (str(model), float(weight), int(resolved.get(model, 0)))
            for model, weight in data["weights"].items()
        ]
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None
    return sorted(rows, key=lambda row: (-row[1], row[0]))


async def read_ledger(
    releases: GitHubReleases, key: bytes, settings: Settings, now: datetime
) -> LedgerReport:
    """Restore the newest snapshot into a temp dir and read the week from it.

    Raises:
        FileNotFoundError: If there is no snapshot or it holds no ledger.
    """
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        if not await restore_latest(releases, data_dir=data_dir, key=key):
            raise FileNotFoundError("no state snapshot")
        if not (data_dir / "ledger.sqlite").exists():
            raise FileNotFoundError("snapshot holds no ledger")
        start = _iso(now - LOOKBACK)
        cycle_start = asknews_cycle_start(now)
        with Ledger(data_dir / "ledger.sqlite") as ledger:
            return LedgerReport(
                runs_by_status=ledger.runs_by_status_since(start),
                degraded_runs=ledger.degraded_runs_since(start),
                models=ledger.model_usage_since(start),
                asknews_used=ledger.asknews_calls_since(_iso(cycle_start)),
                asknews_cap=settings.asknews_monthly_calls,
                cycle_start=cycle_start,
                weights=_read_weights(data_dir / "weights.json"),
            )


def _standing_rows(
    label: str, boards: list[dict[str, Any]] | None
) -> list[tuple[str, ...]]:
    """Rank, score and coverage rows, one column per bot."""
    cells = []
    for bot in BOTS:
        standing = None if boards is None else find_standing(boards, bot.username)
        if boards is None:
            cells.append((UNAVAILABLE,) * 3)
        elif standing is None:
            cells.append((NOT_LISTED,) * 3)
        else:
            cells.append(standing.cells())
    return [
        (f"{label} {name}", *(column[i] for column in cells))
        for i, name in enumerate(("rank", "score", "coverage"))
    ]


async def _posts_forecast_by(
    reader: MetaculusReader, bot: Bot, token: str | None, **filters: Any
) -> list[dict[str, Any]] | None:
    """Posts `bot` forecast (None without its token, or if the read fails)."""
    if not token:
        return None
    return await attempt(
        f"{bot.username} posts",
        reader.posts(token, forecaster_id=bot.user_id, **filters),
    )


async def _finalized_round_rows(
    reader: MetaculusReader,
    token: str | None,
    rounds: tuple[int, ...] | None,
    current: int | None,
) -> list[tuple[str, ...]]:
    """Rows for the newest recorded round that is finalized and not current."""
    label = "Latest finalized round"
    if rounds is None:
        return [(label, UNAVAILABLE, UNAVAILABLE)]
    for round_id in sorted(set(rounds) - {current}, reverse=True):
        boards = await attempt(
            "round leaderboard", reader.leaderboards(round_id, token)
        )
        if boards is None:
            return [(label, UNAVAILABLE, UNAVAILABLE)]
        if round_is_finalized(boards):
            return _standing_rows(f"Round {round_id} (final)", boards)
    return [(label, "none on record", "none on record")]


async def _current_round_row(
    reader: MetaculusReader, tokens: tuple[str | None, str | None], current: int | None
) -> tuple[str, ...]:
    """`forecast of opened` for the current round, one cell per bot."""
    if current is None:
        return ("Current round forecast", UNAVAILABLE, UNAVAILABLE)
    opened = await attempt("round posts", reader.posts(tokens[0], tournaments=current))
    cells = []
    for bot, token in zip(BOTS, tokens, strict=True):
        forecast = await _posts_forecast_by(reader, bot, token, tournaments=current)
        cells.append(
            UNAVAILABLE
            if forecast is None or opened is None
            else f"{len(forecast)} of {len(opened)}"
        )
    return (f"Round {current} forecast", *cells)


async def _recent_forecasts_row(
    reader: MetaculusReader, tokens: tuple[str | None, str | None], now: datetime
) -> tuple[str, ...]:
    """Questions each bot forecast in the last seven days, one cell per bot."""
    cutoff = now - LOOKBACK
    cells = []
    for bot, token in zip(BOTS, tokens, strict=True):
        posts = await _posts_forecast_by(
            reader, bot, token, open_time__gt=cutoff.strftime("%Y-%m-%d")
        )
        cells.append(
            UNAVAILABLE
            if posts is None
            else str(
                sum(
                    1
                    for post in posts
                    if (when := last_forecast_time(post)) is None or when >= cutoff
                )
            )
        )
    return ("Questions forecast, 7 days", *cells)


async def gather_standings(
    client: httpx.AsyncClient,
    settings: Settings,
    releases: GitHubReleases | None,
    now: datetime,
) -> Section:
    """Both bots' FE Fall and MiniBench numbers, one column per bot."""
    reader = MetaculusReader(client)
    tokens = (settings.metaculus_token, settings.v2_metaculus_token)
    fe_fall = await attempt(
        "FE Fall leaderboard",
        reader.leaderboards(FE_FALL_PROJECT_ID, settings.metaculus_token),
    )
    current = await attempt("MiniBench round", reader.minibench_id(tokens[0]))
    stored = (
        None if releases is None else await attempt("watch state", load_state(releases))
    )
    rows = [
        *_standing_rows("FE Fall", fe_fall),
        *await _finalized_round_rows(
            reader, tokens[0], None if stored is None else stored[0].rounds, current
        ),
        await _current_round_row(reader, tokens, current),
        await _recent_forecasts_row(reader, tokens, now),
    ]
    return Section(
        "Standings",
        header=("Measure", VEZO3.username, VEZOCONTROL.username),
        rows=tuple(rows),
    )


def _ledger_sections(report: LedgerReport | None) -> list[Section]:
    titles = ("vezo3 runs, 7 days", "vezo3 models, 7 days", "Model weights", "AskNews")
    if report is None:
        return [unavailable(title) for title in titles]
    runs = Section(
        titles[0],
        notes=(f"Runs with degraded research: {report.degraded_runs}",),
        header=("Status", "Runs"),
        rows=tuple((s, str(n)) for s, n in sorted(report.runs_by_status.items())),
    )
    models = Section(
        titles[1],
        notes=(f"Cost, all models: {_money(sum(m['cost'] for m in report.models))}",),
        header=("Model", "Draws", "OK / attempts", "Cost"),
        rows=tuple(
            (
                m["model_id"],
                str(m["draws"]),
                f"{m['ok']} / {m['attempts']}",
                _money(m["cost"]),
            )
            for m in report.models
        ),
    )
    weights = (
        unavailable(titles[2])
        if report.weights is None
        else Section(
            titles[2],
            header=("Model", "Weight", "Resolved"),
            rows=tuple((m, f"{w:.2f}", str(n)) for m, w, n in report.weights),
        )
    )
    asknews = Section(
        titles[3],
        notes=(
            f"{report.asknews_used} of {report.asknews_cap} calls used this cycle "
            f"(since {report.cycle_start:%Y-%m-%d}).",
        ),
    )
    return [runs, models, weights, asknews]


def _credit_section(
    key_status: KeyStatus | None, now: datetime, window_end: datetime
) -> Section:
    if key_status is None or key_status.limit_remaining is None:
        return unavailable("Credit")
    remaining = key_status.limit_remaining
    weekly = key_status.byok_usage_weekly
    rows = [("Remaining (limit_remaining)", _money(remaining))]
    if weekly is None:
        rows += [
            (name, UNAVAILABLE)
            for name in (
                "Spend (byok_usage_weekly)",
                "Burn per day",
                "Projected run-out",
            )
        ]
    else:
        burn = weekly / 7
        rows += [
            ("Spend (byok_usage_weekly)", _money(weekly)),
            ("Burn per day (weekly / 7)", _money(burn)),
        ]
        if burn > 0:
            run_out = now + timedelta(days=max(remaining, 0) / burn)
            margin = (window_end.date() - run_out.date()).days
            rows += [
                ("Projected run-out", f"{run_out:%Y-%m-%d}"),
                (
                    "Against the last forecasting day",
                    f"{margin} days short"
                    if margin > 0
                    else f"{-margin} days to spare",
                ),
            ]
        else:
            rows.append(("Projected run-out", "no spend this week"))
    rows.append(("Last forecasting day", f"{window_end:%Y-%m-%d}"))
    return Section("Credit", rows=tuple(rows))


def _pool_section(commits: list[dict[str, Any]] | None) -> Section:
    title = "Pool changes, 7 days"
    if commits is None:
        return unavailable(title)
    if not commits:
        return Section(title, notes=(f"No commits touched {POOL_PATH}.",))
    return Section(
        title,
        header=("Date", "Commit", "Subject"),
        rows=tuple(
            (
                str(c["commit"]["committer"]["date"])[:10],
                str(c["sha"])[:7],
                str(c["commit"]["message"]).splitlines()[0],
            )
            for c in commits
        ),
    )


def _waiting_section(
    pulls: list[dict[str, Any]] | None, alerts: list[Alert]
) -> Section:
    rows: list[tuple[str, ...]] = []
    for pull in pulls or []:
        rows.append(
            (f"PR #{pull['number']}", f"{pull['title']} {pull.get('html_url', '')}")
        )
    rows += [("Alert", alert.title) for alert in alerts]
    if pulls is None:
        rows.append(("Open PRs", UNAVAILABLE))
    return Section(
        "Waiting on you",
        notes=() if rows else ("Nothing is waiting.",),
        rows=tuple(rows),
    )


def build_sections(
    *,
    now: datetime,
    window_end: datetime,
    standings: Section,
    ledger: LedgerReport | None,
    obs_pulls: list[dict[str, Any]] | None,
    alerts: list[Alert],
    key_status: KeyStatus | None,
    commits: list[dict[str, Any]] | None,
) -> list[Section]:
    """The summary's sections, top to bottom."""
    return [
        _waiting_section(obs_pulls, alerts),
        standings,
        *_ledger_sections(ledger),
        _credit_section(key_status, now, window_end),
        _pool_section(commits),
    ]


async def run_summary(
    settings: Settings, out: Path | None = None, *, now: datetime | None = None
) -> None:
    """Gather, render and send the weekly summary, or write the HTML to `out`.

    Reads `GITHUB_REPOSITORY`, `GITHUB_TOKEN` and `STATE_KEY` from the
    environment; without them the snapshot sections read "unavailable".

    Args:
        settings: Keys and the recipient.
        out: Write the HTML here instead of sending (for local review).
        now: The current time, for tests. Defaults to the real clock.

    Raises:
        SystemExit: If sending and the mail settings are missing.
        httpx.HTTPError: If the mail cannot be sent.
    """
    now = now or datetime.now(UTC)
    if out is None:
        mail_credentials(settings)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    state_key = os.environ.get("STATE_KEY", "")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        github = GitHubReader(client, token)
        state_releases = GitHubReleases(repo, token, client) if repo and token else None
        watch_releases = (
            GitHubReleases(repo, token, client, tag=WATCH_TAG)
            if repo and token
            else None
        )

        async def read_snapshot() -> LedgerReport | None:
            if state_releases is None or not state_key:
                logger.warning("snapshot unavailable (missing configuration)")
                return None
            return await attempt(
                "snapshot",
                read_ledger(state_releases, state_key.encode(), settings, now),
            )

        ledger, obs, commits, standings = await asyncio.gather(
            read_snapshot(),
            observe(client, settings, token or None, None),
            attempt(
                "pool commits",
                github.commits_touching(BETOMCAT_REPO, POOL_PATH, now - LOOKBACK),
            ),
            gather_standings(client, settings, watch_releases, now),
        )
        alerts = [
            alert
            for alert in evaluate(obs, now)
            if not alert.key.startswith(("pr-", "round-"))
        ]
        title = f"betomcat weekly summary {now:%Y-%m-%d}"
        sections = build_sections(
            now=now,
            window_end=settings.budget_window_end,
            standings=standings,
            ledger=ledger,
            obs_pulls=obs.pulls,
            alerts=alerts,
            key_status=obs.key_status,
            commits=commits,
        )
        html = render_html(title, sections)
        if out is not None:
            out.write_text(html)
        else:
            await send_email(
                client, settings, title, html, render_text(title, sections)
            )
    logger.info(
        "summary: %d sections, %d unavailable",
        len(sections),
        sum(1 for s in sections if s.notes == (UNAVAILABLE,)),
    )
