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

from forecasting_tools.data_models.questions import MetaculusQuestion

from betomcat.pipeline import PipelineDeps, run_pipeline

logger = logging.getLogger(__name__)


async def _forecast_one(question: MetaculusQuestion, deps: PipelineDeps) -> None:
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


async def run_daemon(
    tournaments: tuple[int | str, ...],
    deps: PipelineDeps,
    poll_seconds: int,
) -> None:
    """Poll `tournaments` forever, spawning one pipeline task per new question.

    Args:
        tournaments: Tournament ids/slugs to poll.
        deps: Pipeline dependencies (shared across every question's run).
        poll_seconds: Seconds between polls.
    """
    stop_event = asyncio.Event()
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
                question_id = f"metaculus:{question.id_of_question}"
                if deps.metaculus.already_forecasted(question):
                    continue
                if deps.ledger.has_run(question_id):
                    continue
                task = asyncio.create_task(_forecast_one(question, deps))
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
        await asyncio.gather(*in_flight, return_exceptions=True)
