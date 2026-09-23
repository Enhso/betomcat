"""The GitHub Actions host: one ~5.5h shift of the daemon loop.

Design: `docs/BUILD_LOG.md`, "Answers received 2026-09-22 (evening)". There
is no always-on VM, so the live bot runs as a sequence of long-running
Actions jobs ("shifts"). Each shift:

1. Restores state (ledger, IW corpus, weights, outbox) from an encrypted
   snapshot on the `state` GitHub Release (`betomcat.state`).
2. Starts iw-server as a subprocess and waits for `/health`.
3. Replays any spooled degraded-mode outbox documents into IW.
4. Runs the daemon poll loop, snapshotting after every completed question
   and every 15 minutes.
5. Stops claiming new questions 25 minutes before the shift ends, drains
   in-flight pipelines for another 20 minutes, then abandons whatever is
   still running (the next shift re-picks unforecast questions). Shifts
   shorter than ~83 minutes (test/smoke shifts) scale both margins down to
   30%/10% of the shift instead, so a short shift still gets a real
   claim-then-drain window (see `_shift_margins`).
6. Stops iw-server, takes a final snapshot, and dispatches its successor
   via `workflow_dispatch` so the next shift starts with no dependency on
   cron timing.

`--local` skips every GitHub Release/dispatch call (state stays in
`DATA_DIR` on disk) so the same code path runs unattended on a laptop.

Host mode's Actions logs are public (the repo is public), so
`configure_host_logging` quiets chatty third-party loggers and redacts
any log record from a module that can carry forecast content -- question
ids, tournament, status, timings, and errors are fine; probabilities,
rationales, claims, comment text, and article content are not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from betomcat import state
from betomcat.config import Settings
from betomcat.daemon import DaemonHooks, run_daemon
from betomcat.pipeline import PipelineOutcome

logger = logging.getLogger(__name__)

CLAIM_STOP_BEFORE_END = timedelta(minutes=25)
DRAIN_STOP_BEFORE_END = timedelta(minutes=5)
CLAIM_STOP_FRACTION = 0.3
DRAIN_STOP_FRACTION = 0.1
SNAPSHOT_INTERVAL_SECONDS = 15 * 60
IW_HEALTH_TIMEOUT_SECONDS = 90.0
IW_STOP_TIMEOUT_SECONDS = 20.0
DEFAULT_LLM_MODEL = "google/gemma-4-31b-it:free,openai/gpt-6-luna"

_SENSITIVE_LOGGERS = frozenset(
    {"betomcat.metaculus", "betomcat.comment", "betomcat.forecast", "betomcat.research"}
)
_CHATTY_LOGGERS = ("httpx", "httpcore", "forecasting_tools", "litellm")


class _RedactingFilter(logging.Filter):
    """Blanks the message body of records from forecast-content-bearing loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in _SENSITIVE_LOGGERS:
            record.msg = f"[redacted {record.name} message, level={record.levelname}]"
            record.args = ()
        return True


def configure_host_logging() -> None:
    """Quiet chatty third-party loggers and redact forecast-content loggers.

    The filter is attached directly to each sensitive logger, not to the
    root logger: a `Logger`'s own filters only run for records that
    *originate* there (`Logger.handle` -> `self.filter`); records from a
    child logger that merely propagate up to the root's handlers never
    pass through the root logger's own `.filter()` call, so a root-level
    filter would silently do nothing.
    """
    for name in _CHATTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    redactor = _RedactingFilter()
    for name in _SENSITIVE_LOGGERS:
        logging.getLogger(name).addFilter(redactor)


@dataclass
class _MutableFlag:
    value: bool = False


def _iw_dir() -> Path:
    return Path(os.environ.get("IW_DIR", "./iw")).resolve()


def _iw_bind(iw_url: str) -> str:
    parsed = urlparse(iw_url)
    return f"{parsed.hostname or '127.0.0.1'}:{parsed.port or 8080}"


def _iw_env(data_dir: Path, iw_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "IW_DB_ENGINE": "sqlite",
            "IW_DB_PATH": str(data_dir / "iw.sqlite"),
            "IW_BIND": _iw_bind(iw_url),
            "IW_PYTHON_DIR": str(_iw_dir() / "python"),
            "RUST_LOG": "warn",
            "LLM_API_BASE": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
            "LLM_MODEL": os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
        }
    )
    return env


async def _wait_for_health(
    iw_url: str, timeout_seconds: float, client: httpx.AsyncClient
) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
    last_error: Exception | None = None
    while datetime.now(UTC) < deadline:
        try:
            response = await client.get(f"{iw_url}/health", timeout=5)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        await asyncio.sleep(1.0)
    raise RuntimeError(f"iw-server did not become healthy in time: {last_error}")


async def _stop_iw(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=IW_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("iw-server did not stop gracefully within timeout, killing")
        proc.kill()
        await proc.wait()


async def _dispatch_successor(
    repo: str, token: str, client: httpx.AsyncClient, flag_path: Path
) -> None:
    """Trigger the next shift via `workflow_dispatch` and record having done so."""
    response = await client.post(
        f"{state.GITHUB_API}/repos/{repo}/actions/workflows/host.yml/dispatches",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main"},
        timeout=30,
    )
    response.raise_for_status()
    flag_path.write_text(datetime.now(UTC).isoformat())
    logger.info("successor shift dispatched")


async def _shift_timer(
    *,
    claim_stop_at: datetime,
    drain_stop_at: datetime,
    stop_claiming: _MutableFlag,
    stop_event: asyncio.Event,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Flip `stop_claiming` at `claim_stop_at`, set `stop_event` at `drain_stop_at`."""
    while clock() < claim_stop_at and not stop_event.is_set():
        await sleep(min(5.0, (claim_stop_at - clock()).total_seconds()))
    stop_claiming.value = True

    while clock() < drain_stop_at and not stop_event.is_set():
        await sleep(min(5.0, (drain_stop_at - clock()).total_seconds()))
    stop_event.set()


async def _periodic_snapshot(
    *, stop_event: asyncio.Event, snapshot: Callable[[], Awaitable[None]]
) -> None:
    while not stop_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=SNAPSHOT_INTERVAL_SECONDS)
        if not stop_event.is_set():
            await snapshot()


def _dispatch_flag_path() -> Path:
    return Path(os.environ.get("DISPATCH_FLAG_PATH", ".betomcat-dispatched"))


def _shift_margins(shift_minutes: int) -> tuple[timedelta, timedelta]:
    """Claim-stop and drain-stop margins before shift end, scaled for short shifts.

    Production shifts (330min) get the fixed `CLAIM_STOP_BEFORE_END` (25min)
    and `DRAIN_STOP_BEFORE_END` (5min) margins. A shift shorter than
    ~83 minutes would otherwise have both margins land before the shift even
    starts, so a test/smoke shift never claims anything; below that length
    the margins scale down to a fraction of the shift instead (30% / 10%),
    so a short shift still gets a real claim-then-drain window.
    """
    shift = timedelta(minutes=shift_minutes)
    claim_margin = min(CLAIM_STOP_BEFORE_END, shift * CLAIM_STOP_FRACTION)
    drain_margin = min(DRAIN_STOP_BEFORE_END, shift * DRAIN_STOP_FRACTION)
    return claim_margin, drain_margin


async def run_host(
    settings: Settings,
    *,
    shift_minutes: int,
    local: bool,
    max_questions: int | None = None,
) -> None:
    """Run one host shift: restore, serve, snapshot, dispatch the successor."""
    configure_host_logging()
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    state_key = os.environ.get("STATE_KEY", "").encode()

    if not local and not (repo and github_token and state_key):
        raise RuntimeError(
            "host mode requires GITHUB_REPOSITORY, GITHUB_TOKEN, and STATE_KEY "
            "(pass --local to run against local DATA_DIR only)"
        )

    releases = state.GitHubReleases(repo, github_token) if not local else None
    try:
        if releases is not None:
            await state.restore_latest(releases, data_dir=data_dir, key=state_key)

        iw_binary = _iw_dir() / "target" / "release" / "iw-server"
        if not iw_binary.exists():
            raise RuntimeError(f"iw-server binary not found at {iw_binary}")

        iw_proc = await asyncio.create_subprocess_exec(
            str(iw_binary),
            cwd=str(_iw_dir()),
            env=_iw_env(data_dir, settings.iw_url),
        )
        logger.info("iw-server started: pid=%d", iw_proc.pid)

        stop_event = asyncio.Event()
        snapshot_lock = asyncio.Lock()
        background_tasks: set[asyncio.Task[None]] = set()

        async def do_snapshot() -> None:
            if releases is None:
                return
            async with snapshot_lock:
                name = await state.snapshot_and_upload(
                    releases,
                    data_dir=data_dir,
                    iw_db_path=data_dir / "iw.sqlite",
                    key=state_key,
                )
                logger.info("shift snapshot recorded: %s", name)

        def _handle_signal() -> None:
            logger.warning("termination signal received, snapshotting before exit")
            stop_event.set()
            task = asyncio.ensure_future(do_snapshot())
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _handle_signal)

        try:
            async with httpx.AsyncClient() as health_client:
                await _wait_for_health(
                    settings.iw_url, IW_HEALTH_TIMEOUT_SECONDS, health_client
                )
            logger.info("iw-server healthy")

            # Deferred import: cli.py imports `run_host` from this module, so
            # importing it back at module scope would be circular.
            from betomcat.cli import build_deps, replay_outbox

            deps = build_deps(settings, settings.dry_run)
            try:
                replayed = await replay_outbox(deps.iw, data_dir)
                logger.info("outbox replay: %d document(s) ingested", replayed)

                shift_end = datetime.now(UTC) + timedelta(minutes=shift_minutes)
                claim_margin, drain_margin = _shift_margins(shift_minutes)
                claim_stop_at = shift_end - claim_margin
                drain_stop_at = shift_end - drain_margin
                stop_claiming = _MutableFlag()
                done_count = {"n": 0}
                claimed_count = {"n": 0}

                def should_claim() -> bool:
                    """Gates each individual claim, not just each poll.

                    `run_daemon` calls this once per question about to be
                    claimed, so counting here (rather than in
                    `on_question_done`, which only fires after a question
                    finishes) is what actually caps `--max-questions` at
                    claim time -- a single poll can otherwise return many
                    questions before any of them complete.
                    """
                    if stop_claiming.value:
                        return False
                    if (
                        max_questions is not None
                        and claimed_count["n"] >= max_questions
                    ):
                        return False
                    claimed_count["n"] += 1
                    return True

                async def on_question_done(
                    question_id: str, outcome: PipelineOutcome
                ) -> None:
                    logger.info(
                        "question %s done: status=%s", question_id, outcome.status
                    )
                    done_count["n"] += 1
                    await do_snapshot()
                    if max_questions is not None and done_count["n"] >= max_questions:
                        stop_claiming.value = True
                        stop_event.set()

                timer_task = asyncio.create_task(
                    _shift_timer(
                        claim_stop_at=claim_stop_at,
                        drain_stop_at=drain_stop_at,
                        stop_claiming=stop_claiming,
                        stop_event=stop_event,
                        clock=lambda: datetime.now(UTC),
                        sleep=asyncio.sleep,
                    )
                )
                snapshot_task = asyncio.create_task(
                    _periodic_snapshot(stop_event=stop_event, snapshot=do_snapshot)
                )
                try:
                    drain_budget = max(0.0, (shift_end - drain_stop_at).total_seconds())
                    await run_daemon(
                        settings.tournaments,
                        deps,
                        settings.poll_seconds,
                        DaemonHooks(
                            stop_event=stop_event,
                            should_claim=should_claim,
                            on_question_done=on_question_done,
                            drain_timeout=drain_budget,
                            install_signal_handlers=False,
                        ),
                    )
                finally:
                    for task in (timer_task, snapshot_task):
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            finally:
                await deps.iw.aclose()
                await deps.llm.aclose()
                deps.ledger.close()
        finally:
            await _stop_iw(iw_proc)
            logger.info("iw-server stopped")
            await do_snapshot()

        if releases is not None:
            async with httpx.AsyncClient() as gh_client:
                await _dispatch_successor(
                    repo, github_token, gh_client, _dispatch_flag_path()
                )
    finally:
        if releases is not None:
            await releases.aclose()
