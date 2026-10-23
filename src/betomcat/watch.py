"""`betomcat watch`: hourly alerts, one mail for the keys not sent before.

Each condition has a key; a key already in the watch state is not mailed
again. The state (`sent` keys and the MiniBench `rounds` seen) is a small
JSON asset on a draft release tagged `watch`, apart from the host's `state`
release. It is not sensitive, so it is not encrypted.

A source that cannot be read suppresses its own condition and never fires
it: a GitHub 500 is not a dead chain. An unreadable state sends nothing and
writes nothing, so a read failure cannot resend every alert.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson

from betomcat.budget import KeyStatus, fetch_key_status
from betomcat.config import Settings
from betomcat.mail import mail_credentials, send_email
from betomcat.report import Section, render_html, render_text
from betomcat.sources import (
    BETOMCAT_REPO,
    CONTROL_WORKFLOW,
    HOST_WORKFLOW,
    SOURCE_ERRORS,
    VEZO3,
    VEZOCONTROL,
    VEZOCONTROL_REPO,
    GitHubReader,
    MetaculusReader,
    attempt,
    find_standing,
    round_is_finalized,
)
from betomcat.state import KEEP_ASSETS, GitHubReleases

logger = logging.getLogger(__name__)

WATCH_TAG = "watch"
SEED_ROUNDS = (33132,)
STALL_AFTER = timedelta(minutes=45)
CREDIT_RESET_USD = 15.0
CREDIT_THRESHOLDS = ((15.0, "credit-15"), (5.0, "credit-5"))


@dataclass(frozen=True)
class Alert:
    """One condition that holds now: its dedupe key, subject text and detail."""

    key: str
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class WatchState:
    """Keys already mailed and the MiniBench project ids seen so far."""

    sent: frozenset[str]
    rounds: tuple[int, ...]


@dataclass(frozen=True)
class Observations:
    """What the sources said this run; None marks a source that failed."""

    host_runs: list[dict[str, Any]] | None = None
    control_runs: list[dict[str, Any]] | None = None
    failed_steps: tuple[str, ...] = ()
    key_status: KeyStatus | None = None
    pulls: list[dict[str, Any]] | None = None
    current_round: int | None = None
    round_boards: dict[int, list[dict[str, Any]]] = field(default_factory=dict)


def newest_completed(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The completed run with the highest id, if any."""
    return max(
        (run for run in runs if run.get("status") == "completed"),
        key=lambda run: int(run["id"]),
        default=None,
    )


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _chain_alerts(
    version: str,
    repo: str,
    workflow: str,
    runs: list[dict[str, Any]] | None,
    now: datetime,
) -> list[Alert]:
    if not runs or any(run.get("status") != "completed" for run in runs):
        return []
    newest = max(runs, key=lambda run: int(run["id"]))
    finished = _parse_time(newest["updated_at"])
    if now - finished <= STALL_AFTER:
        return []
    minutes = int((now - finished).total_seconds() // 60)
    return [
        Alert(
            key=f"chain-{version}-{newest['id']}",
            title=f"{version} chain stalled ({workflow})",
            lines=(
                f"No {workflow} run is queued or in progress on {repo}.",
                f"The newest, run {newest['id']}, finished {minutes} min ago "
                f"({newest.get('conclusion')}).",
                str(newest.get("html_url", "")),
            ),
        )
    ]


def _failed_alerts(
    runs: list[dict[str, Any]] | None, steps: tuple[str, ...]
) -> list[Alert]:
    newest = newest_completed(runs or [])
    if newest is None or newest.get("conclusion") != "failure":
        return []
    return [
        Alert(
            key=f"failed-{newest['id']}",
            title=f"{HOST_WORKFLOW} run {newest['id']} failed",
            lines=(
                f"Failed step: {', '.join(steps) or 'unknown'}",
                str(newest.get("html_url", "")),
            ),
        )
    ]


def _credit_alerts(key_status: KeyStatus | None) -> list[Alert]:
    if key_status is None or key_status.limit_remaining is None:
        return []
    remaining = key_status.limit_remaining
    return [
        Alert(
            key=key,
            title=f"credit below ${threshold:.0f} (${remaining:.2f} left)",
            lines=(f"OpenRouter limit_remaining is ${remaining:.2f}.",),
        )
        for threshold, key in CREDIT_THRESHOLDS
        if remaining < threshold
    ]


def _pull_alerts(pulls: list[dict[str, Any]] | None) -> list[Alert]:
    return [
        Alert(
            key=f"pr-{pull['number']}-{pull['head']['sha']}",
            title=f"PR #{pull['number']} waiting: {pull['title']}",
            lines=(str(pull.get("html_url", "")),),
        )
        for pull in pulls or []
    ]


def _round_alerts(boards_by_round: dict[int, list[dict[str, Any]]]) -> list[Alert]:
    alerts = []
    for round_id, boards in sorted(boards_by_round.items()):
        if not round_is_finalized(boards):
            continue
        lines = []
        for bot in (VEZO3, VEZOCONTROL):
            standing = find_standing(boards, bot.username)
            lines.append(
                f"{bot.username}: "
                + ("not listed" if standing is None else standing.describe())
            )
        alerts.append(
            Alert(
                key=f"round-{round_id}",
                title=f"MiniBench round {round_id} finalized",
                lines=tuple(lines),
            )
        )
    return alerts


def evaluate(obs: Observations, now: datetime) -> list[Alert]:
    """Every condition that holds now, in the order the mail lists them."""
    return [
        *_chain_alerts("v1", BETOMCAT_REPO, HOST_WORKFLOW, obs.host_runs, now),
        *_chain_alerts("v2", VEZOCONTROL_REPO, CONTROL_WORKFLOW, obs.control_runs, now),
        *_failed_alerts(obs.host_runs, obs.failed_steps),
        *_credit_alerts(obs.key_status),
        *_pull_alerts(obs.pulls),
        *_round_alerts(obs.round_boards),
    ]


def select_new(
    alerts: list[Alert], state: WatchState, limit_remaining: float | None
) -> tuple[list[Alert], WatchState]:
    """The alerts not sent before, and the state once they are.

    Credit back at $15 or more clears both credit keys so a later drop alerts
    again. An unknown credit (`None`) clears nothing.
    """
    sent = set(state.sent)
    if limit_remaining is not None and limit_remaining >= CREDIT_RESET_USD:
        sent -= {key for _, key in CREDIT_THRESHOLDS}
    new = [alert for alert in alerts if alert.key not in sent]
    return new, replace(state, sent=frozenset(sent | {a.key for a in new}))


def compose(alerts: list[Alert]) -> tuple[str, str, str]:
    """Subject, HTML and text for one mail listing all of `alerts`."""
    subject = f"betomcat: {alerts[0].title}"
    if len(alerts) > 1:
        subject += f" (+{len(alerts) - 1} more)"
    sections = [Section(alert.title, notes=alert.lines) for alert in alerts]
    return (
        subject,
        render_html("betomcat alerts", sections),
        render_text("betomcat alerts", sections),
    )


async def load_state(releases: GitHubReleases) -> tuple[WatchState, bool]:
    """The stored watch state, and whether one was stored.

    No stored asset is a fresh start (seeded with `SEED_ROUNDS`), not a
    failure.

    Raises:
        httpx.HTTPError: If the release or the asset cannot be read.
        ValueError: If the stored JSON has the wrong shape.
    """
    _release_id, assets = await releases.get_or_create_release()
    if not assets:
        return WatchState(frozenset(), SEED_ROUNDS), False
    latest = max(assets, key=lambda asset: asset.name)
    data = orjson.loads(await releases.download_asset(latest.id))
    if not isinstance(data["sent"], list) or not isinstance(data["rounds"], list):
        raise ValueError("watch state has the wrong shape")
    return (
        WatchState(
            frozenset(str(key) for key in data["sent"]),
            tuple(int(round_id) for round_id in data["rounds"]),
        ),
        True,
    )


async def save_state(
    releases: GitHubReleases, state: WatchState, now: datetime
) -> None:
    """Upload `state` as a new asset, then keep only the newest few."""
    release_id, existing = await releases.get_or_create_release()
    payload = orjson.dumps({"sent": sorted(state.sent), "rounds": list(state.rounds)})
    uploaded = await releases.upload_asset(
        release_id, f"watch-{now.strftime('%Y%m%dT%H%M%SZ')}.json", payload
    )
    newest_first = sorted([*existing, uploaded], key=lambda a: a.name, reverse=True)
    for stale in newest_first[KEEP_ASSETS:]:
        await releases.delete_asset(stale.id)


async def observe(
    client: httpx.AsyncClient,
    settings: Settings,
    github_token: str | None,
    state: WatchState | None,
) -> Observations:
    """Read every source once; a failed source comes back as None.

    Args:
        client: The HTTP client.
        settings: Supplies the Metaculus and OpenRouter keys.
        github_token: Token for the GitHub reads.
        state: The watch state, or None to skip the MiniBench round checks
            (the weekly summary does).
    """
    github = GitHubReader(client, github_token)
    host_runs = await attempt(
        "host runs", github.workflow_runs(BETOMCAT_REPO, HOST_WORKFLOW)
    )
    control_runs = await attempt(
        "control runs", github.workflow_runs(VEZOCONTROL_REPO, CONTROL_WORKFLOW)
    )
    failed_steps: tuple[str, ...] = ()
    newest = newest_completed(host_runs or [])
    if newest is not None and newest.get("conclusion") == "failure":
        failed_steps = (
            await attempt(
                "failed steps", github.failed_steps(BETOMCAT_REPO, int(newest["id"]))
            )
            or ()
        )
    key_status = None
    if settings.openrouter_api_key:
        status = await fetch_key_status(client, settings.openrouter_api_key)
        key_status = status if status.ok else None
    pulls = await attempt("open PRs", github.open_pulls(BETOMCAT_REPO))

    current_round = None
    round_boards: dict[int, list[dict[str, Any]]] = {}
    if state is not None:
        metaculus = MetaculusReader(client)
        token = settings.metaculus_token
        current_round = await attempt("MiniBench round", metaculus.minibench_id(token))
        rounds = set(state.rounds) | ({current_round} if current_round else set())
        for round_id in sorted(rounds):
            if f"round-{round_id}" in state.sent:
                continue
            boards = await attempt(
                "round leaderboard", metaculus.leaderboards(round_id, token)
            )
            if boards is not None:
                round_boards[round_id] = boards
    return Observations(
        host_runs=host_runs,
        control_runs=control_runs,
        failed_steps=failed_steps,
        key_status=key_status,
        pulls=pulls,
        current_round=current_round,
        round_boards=round_boards,
    )


async def run_watch(settings: Settings, *, now: datetime | None = None) -> None:
    """One watch run: read the sources, mail the new alerts, store the state.

    Reads `GITHUB_REPOSITORY` and `GITHUB_TOKEN` from the environment. The
    mail goes first and the keys are stored after it, so a failed send is
    retried next hour and a failed store can only repeat a mail.

    Raises:
        SystemExit: If the GitHub or mail settings are missing.
        httpx.HTTPError: If the mail or the state write fails.
    """
    now = now or datetime.now(UTC)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not (repo and token):
        raise SystemExit("watch requires GITHUB_REPOSITORY and GITHUB_TOKEN")
    mail_credentials(settings)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        releases = GitHubReleases(repo, token, client, tag=WATCH_TAG)
        try:
            stored, existed = await load_state(releases)
        except SOURCE_ERRORS as exc:
            logger.error(
                "watch state read failed (%s); sending nothing", type(exc).__name__
            )
            return
        obs = await observe(client, settings, token, stored)
        state = replace(
            stored,
            rounds=tuple(sorted({*stored.rounds, *filter(None, [obs.current_round])})),
        )
        alerts = evaluate(obs, now)
        remaining = obs.key_status.limit_remaining if obs.key_status else None
        new, new_state = select_new(alerts, state, remaining)
        if new:
            subject, html, text = compose(new)
            await send_email(client, settings, subject, html, text)
        if new_state != stored or not existed:
            await save_state(releases, new_state, now)
        logger.info("watch: %d active, %d new", len(alerts), len(new))
