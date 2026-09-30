"""The always-on poll loop: fetch open tournament questions, forecast new ones.

Runs forever, polling every `Settings.poll_seconds`. One question's pipeline
exception never kills the loop (`run_pipeline` already catches and records
`failed` internally; this loop's own try/except is a second backstop). Shuts
down cleanly on SIGTERM/SIGINT.

Spot scoring only counts the last forecast before close, and the bot
forecasts each question once, so a long-window question must not be claimed
at open -- it would be weeks stale at close. `run_daemon` only claims a
question once it's within `LATE_WINDOW_MINUTES` of its scheduled close (env,
default 180); MiniBench's 3h windows are claimed right away. A question
claimed too early is simply reconsidered on a later poll -- no state needed.

At most `MAX_CONCURRENT_QUESTIONS` (env, default 4) pipelines run at once, so
a burst of openings queues instead of launching every research job together.
The poll loop wakes early when an in-flight question finishes and others are
waiting, so a freed slot is refilled at once rather than after `poll_seconds`.

[PRACTICE] questions are never forecast (Hatim, 2026-09-24).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from forecasting_tools.data_models.questions import MetaculusQuestion

from betomcat.pipeline import PipelineDeps, PipelineOutcome, run_pipeline

logger = logging.getLogger(__name__)

DEFAULT_LATE_WINDOW_MINUTES = 180
DEFAULT_MAX_CONCURRENT_QUESTIONS = 4
PRACTICE_PREFIX = "[PRACTICE]"


def _late_window_minutes() -> int:
    """`LATE_WINDOW_MINUTES` env var, or `DEFAULT_LATE_WINDOW_MINUTES`."""
    raw = os.environ.get("LATE_WINDOW_MINUTES")
    if raw is None or raw.strip() == "":
        return DEFAULT_LATE_WINDOW_MINUTES
    return int(raw)


def _max_concurrent_questions() -> int:
    """`MAX_CONCURRENT_QUESTIONS` env var, or `DEFAULT_MAX_CONCURRENT_QUESTIONS`."""
    raw = os.environ.get("MAX_CONCURRENT_QUESTIONS")
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_CONCURRENT_QUESTIONS
    cap = int(raw)
    if cap < 1:
        raise ValueError(f"MAX_CONCURRENT_QUESTIONS must be >= 1, got {cap}")
    return cap


async def _wait_for_next_poll(
    stop_event: asyncio.Event, slot_freed: asyncio.Event | None, poll_seconds: float
) -> None:
    """Sleep until `stop_event`, `slot_freed` (if given) or `poll_seconds` elapses."""
    waiters = [asyncio.create_task(stop_event.wait())]
    if slot_freed is not None:
        waiters.append(asyncio.create_task(slot_freed.wait()))
    try:
        await asyncio.wait(
            waiters, timeout=poll_seconds, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for waiter in waiters:
            waiter.cancel()


@dataclass
class DaemonHooks:
    """Optional extensions to `run_daemon`'s poll loop, used by the host shift.

    Attributes:
        stop_event: External stop signal to drive shutdown with, instead of
            one `run_daemon` creates itself. Lets a caller (the GitHub
            Actions host) force shutdown by setting it directly.
        should_claim: Checked once per question considered for claiming
            (not once per poll -- a caller that counts `True` results to
            enforce a claim cap would otherwise be consulted an extra time
            per poll and undercount by one). Once it returns false, no more
            questions are claimed that poll (existing in-flight runs keep
            going); the loop still polls until `stop_event` is set.
        on_question_done: Awaited after each question's pipeline finishes,
            with its ledger question id and outcome. Exceptions are logged,
            never propagated.
        drain_timeout: Seconds to wait for in-flight questions to finish
            once shutdown starts, before abandoning the rest. `None` waits
            indefinitely.
        install_signal_handlers: Whether to install SIGTERM/SIGINT handlers
            on `stop_event`. A given signal can only have one handler per
            event loop, so a caller installing its own must pass `False`.
        now: Clock used for the late-window claim check. Injectable for
            tests; defaults to the real UTC clock.
    """

    stop_event: asyncio.Event | None = None
    should_claim: Callable[[], bool] = lambda: True
    on_question_done: Callable[[str, PipelineOutcome], Awaitable[None]] | None = None
    drain_timeout: float | None = None
    install_signal_handlers: bool = True
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


async def _forecast_one(
    question: MetaculusQuestion,
    deps: PipelineDeps,
    on_done: Callable[[str, PipelineOutcome], Awaitable[None]] | None,
) -> None:
    question_id = f"metaculus:{question.id_of_question}"
    try:
        outcome = await run_pipeline(question, deps)
        logger.info(
            "question %s finished: status=%s detail=%s",
            question.id_of_question,
            outcome.status,
            outcome.detail,
        )
    except Exception:
        logger.exception(
            "unhandled exception forecasting question %s", question.id_of_question
        )
        outcome = PipelineOutcome("failed", None, "unhandled exception")
    if on_done is not None:
        try:
            await on_done(question_id, outcome)
        except Exception:
            logger.exception("on_question_done callback failed for %s", question_id)


async def run_daemon(
    tournaments: tuple[int | str, ...],
    deps: PipelineDeps,
    poll_seconds: int,
    hooks: DaemonHooks | None = None,
) -> None:
    """Poll `tournaments` forever, spawning one pipeline task per new question.

    Args:
        tournaments: Tournament ids/slugs to poll.
        deps: Pipeline dependencies (shared across every question's run).
        poll_seconds: Seconds between polls.
        hooks: Optional host-mode extensions; see `DaemonHooks`.
    """
    hooks = hooks or DaemonHooks()
    stop_event = hooks.stop_event if hooks.stop_event is not None else asyncio.Event()
    late_window_minutes = _late_window_minutes()
    max_concurrent = _max_concurrent_questions()

    if hooks.install_signal_handlers:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Signal handlers aren't available on every platform (e.g. some
            # test/event-loop setups); the daemon just won't catch that signal.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

    in_flight: set[asyncio.Task[None]] = set()
    # Every question id this process has started a pipeline for. `has_run`
    # only covers a question once its pipeline has written a run row, which
    # happens after the task starts; and a pipeline that skips a question
    # (unsupported type) never writes one. Without this set, the early
    # re-poll below could start the same question again, or spin on a
    # question that finishes instantly without a run row.
    claimed_ids: set[str] = set()
    slot_freed = asyncio.Event()

    def _release_slot(task: asyncio.Task[None]) -> None:
        in_flight.discard(task)
        slot_freed.set()

    while not stop_event.is_set():
        slot_freed.clear()
        deferred = 0
        practice = 0
        waiting = 0
        for tournament in tournaments:
            try:
                questions = await deps.metaculus.list_open_questions(tournament)
            except Exception:
                logger.exception("failed to list open questions for %s", tournament)
                continue

            for question in questions:
                if question.question_text.startswith(PRACTICE_PREFIX):
                    practice += 1
                    continue
                # The late-window check comes before `should_claim` so a
                # question that isn't claimable yet never consumes a claim
                # slot from a caller enforcing a cap (host.py's
                # --max-questions) -- the same failure mode as the
                # should_claim double-count this loop already guards
                # against below.
                close_time = question.close_time
                if close_time is None:
                    logger.info(
                        "question %s has no close time; skipping",
                        question.id_of_question,
                    )
                    continue
                minutes_to_close = (close_time - hooks.now()).total_seconds() / 60.0
                if minutes_to_close > late_window_minutes:
                    deferred += 1
                    continue

                question_id = f"metaculus:{question.id_of_question}"
                if (
                    question_id in claimed_ids
                    or deps.metaculus.already_forecasted(question)
                    or deps.ledger.has_run(question_id)
                ):
                    continue

                # The concurrency cap also comes before `should_claim`, for
                # the same reason as the late-window check: a question that
                # cannot start now must not consume a claim slot.
                if len(in_flight) >= max_concurrent:
                    waiting += 1
                    continue

                # `should_claim` is called exactly once per question actually
                # claimed -- a caller enforcing a claim cap (e.g. host.py's
                # --max-questions) counts True results, so calling it more
                # than once per real claim (an outer per-poll gate, or for
                # questions that turn out to be already run) would exhaust
                # the cap without ever claiming anything.
                if not hooks.should_claim():
                    break
                claimed_ids.add(question_id)
                task = asyncio.create_task(
                    _forecast_one(question, deps, hooks.on_question_done)
                )
                in_flight.add(task)
                task.add_done_callback(_release_slot)

        logger.info(
            "heartbeat: poll complete, %d question(s) in flight (cap %d), "
            "%d waiting for a slot, "
            "%d open question(s) deferred (outside the %d-minute late window), "
            "%d practice question(s) skipped",
            len(in_flight),
            max_concurrent,
            waiting,
            deferred,
            late_window_minutes,
            practice,
        )

        await _wait_for_next_poll(
            stop_event, slot_freed if waiting else None, poll_seconds
        )

    logger.info(
        "shutdown requested, waiting on %d in-flight question(s)", len(in_flight)
    )
    if in_flight:
        gathered = asyncio.gather(*in_flight, return_exceptions=True)
        try:
            await asyncio.wait_for(gathered, timeout=hooks.drain_timeout)
        except TimeoutError:
            still_pending = sum(1 for task in in_flight if not task.done())
            logger.warning(
                "drain timeout hit with %d question(s) still in flight; abandoning",
                still_pending,
            )
