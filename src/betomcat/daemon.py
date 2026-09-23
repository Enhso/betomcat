"""The always-on poll loop: fetch open tournament questions, forecast new ones.

Runs forever, polling every `Settings.poll_seconds`. One question's pipeline
exception never kills the loop (`run_pipeline` already catches and records
`failed` internally; this loop's own try/except is a second backstop). Shuts
down cleanly on SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from forecasting_tools.data_models.questions import MetaculusQuestion

from betomcat.pipeline import PipelineDeps, PipelineOutcome, run_pipeline

logger = logging.getLogger(__name__)


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
    """

    stop_event: asyncio.Event | None = None
    should_claim: Callable[[], bool] = lambda: True
    on_question_done: Callable[[str, PipelineOutcome], Awaitable[None]] | None = None
    drain_timeout: float | None = None
    install_signal_handlers: bool = True


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

    if hooks.install_signal_handlers:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Signal handlers aren't available on every platform (e.g. some
            # test/event-loop setups); the daemon just won't catch that signal.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

    in_flight: set[asyncio.Task[None]] = set()

    while not stop_event.is_set():
        for tournament in tournaments:
            try:
                questions = await deps.metaculus.list_open_questions(tournament)
            except Exception:
                logger.exception("failed to list open questions for %s", tournament)
                continue

            for question in questions:
                # `should_claim` is called exactly once per question actually
                # considered for claiming -- a caller enforcing a claim cap
                # (e.g. host.py's --max-questions) counts True results, so
                # calling it more than once per real claim (an outer
                # per-poll gate plus this one) would exhaust the cap without
                # ever claiming anything.
                if not hooks.should_claim():
                    break
                question_id = f"metaculus:{question.id_of_question}"
                if deps.metaculus.already_forecasted(question):
                    continue
                if deps.ledger.has_run(question_id):
                    continue
                task = asyncio.create_task(
                    _forecast_one(question, deps, hooks.on_question_done)
                )
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)

        logger.info(
            "heartbeat: poll complete, %d question(s) in flight", len(in_flight)
        )

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)

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
