"""Tests for `run_daemon`'s host-mode hooks (`DaemonHooks`) and late-window
claiming (`C3c`)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from betomcat import daemon as daemon_module
from betomcat.daemon import DaemonHooks, run_daemon
from betomcat.pipeline import PipelineDeps, PipelineOutcome


@dataclass
class FakeMetaculus:
    questions: list[Any]
    forecasted: set[str] = field(default_factory=set)

    async def list_open_questions(self, tournament: object) -> list[Any]:
        return list(self.questions)

    def already_forecasted(self, question: Any) -> bool:
        return question.id_of_question in self.forecasted


@dataclass
class FakeLedger:
    runs: set[str] = field(default_factory=set)

    def has_run(self, question_id: str) -> bool:
        return question_id in self.runs


def _question(
    qid: str,
    close_time: datetime | None = None,
    question_text: str = "Will it happen?",
) -> SimpleNamespace:
    # Default close time sits well inside the default 180-minute late
    # window, so tests unrelated to late-window claiming keep claiming
    # immediately without each having to set one.
    if close_time is None:
        close_time = datetime.now(UTC) + timedelta(minutes=30)
    return SimpleNamespace(
        id_of_question=qid, close_time=close_time, question_text=question_text
    )


def _deps(metaculus: FakeMetaculus, ledger: FakeLedger, tmp_path: Path) -> PipelineDeps:
    return PipelineDeps(
        iw=object(),  # type: ignore[arg-type]
        llm=object(),  # type: ignore[arg-type]
        metaculus=metaculus,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        pool_path=tmp_path / "models.yaml",
        data_dir=tmp_path,
    )


async def test_should_claim_false_skips_new_questions_but_keeps_polling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(FakeMetaculus(questions=[_question("1")]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                should_claim=lambda: False,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )
    await stopper

    assert calls == []


async def test_should_claim_is_called_once_per_question_not_once_per_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression test: a caller enforcing a claim cap (host.py's
    --max-questions) counts `True` results from `should_claim`. If
    `run_daemon` consulted it an extra time per poll (e.g. an outer gate
    plus the per-question check), a cap of 1 would be exhausted before any
    question was actually claimed -- exactly what happened in the first
    end-to-end host smoke test."""
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(FakeMetaculus(questions=[_question("1")]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()
    claimed = {"n": 0}

    def should_claim_capped_at_one() -> bool:
        if claimed["n"] >= 1:
            return False
        claimed["n"] += 1
        return True

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        stop_event.set()

    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                should_claim=should_claim_capped_at_one,
                on_question_done=on_done,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )

    assert calls == ["1"]


async def test_on_question_done_called_with_question_id_and_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        return PipelineOutcome("submitted", 42)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(FakeMetaculus(questions=[_question("1")]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()
    done: list[tuple[str, PipelineOutcome]] = []

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        done.append((question_id, outcome))
        stop_event.set()

    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                on_question_done=on_done,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )

    assert done == [("metaculus:1", PipelineOutcome("submitted", 42))]


async def test_drain_timeout_abandons_a_still_running_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        started.set()
        await asyncio.sleep(10)  # never completes within the drain budget below
        return PipelineOutcome("submitted", 1)  # pragma: no cover

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(FakeMetaculus(questions=[_question("1")]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def stop_once_in_flight() -> None:
        await started.wait()
        stop_event.set()

    stopper = asyncio.create_task(stop_once_in_flight())

    # If drain_timeout weren't honoured, this would hang ~10s and the
    # outer wait_for would raise TimeoutError.
    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                drain_timeout=0.05,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )
    await stopper


# -- late-window claiming (C3c) -------------------------------------------------

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


async def test_late_window_defers_a_question_whose_close_is_far_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 22-day-window practice question (the real FE Fall 2026 case that
    motivated this fix) must not be claimed at open."""
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    far_question = _question("1", close_time=NOW + timedelta(days=22))
    deps = _deps(FakeMetaculus(questions=[far_question]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event, now=lambda: NOW, install_signal_handlers=False
            ),
        ),
        timeout=5,
    )
    await stopper

    assert calls == []


async def test_late_window_claims_a_question_within_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A MiniBench-style 3h-window question is claimed right away: at
    DEFAULT_LATE_WINDOW_MINUTES=180 it's always within the late window."""
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    soon_question = _question("1", close_time=NOW + timedelta(hours=3))
    deps = _deps(FakeMetaculus(questions=[soon_question]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        stop_event.set()

    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                now=lambda: NOW,
                on_question_done=on_done,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )

    assert calls == ["1"]


async def test_late_window_env_var_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LATE_WINDOW_MINUTES", "2000")
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    # Outside the default 180-minute window but inside the overridden 2000.
    question = _question("1", close_time=NOW + timedelta(minutes=1000))
    deps = _deps(FakeMetaculus(questions=[question]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        stop_event.set()

    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                now=lambda: NOW,
                on_question_done=on_done,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )

    assert calls == ["1"]


async def test_questions_without_close_time_are_skipped_and_logged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    no_close_time = SimpleNamespace(
        id_of_question="1", close_time=None, question_text="Will it happen?"
    )
    deps = _deps(FakeMetaculus(questions=[no_close_time]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    caplog.set_level(logging.INFO, logger=daemon_module.__name__)
    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(stop_event=stop_event, install_signal_handlers=False),
        ),
        timeout=5,
    )
    await stopper

    assert calls == []
    assert "question 1 has no close time; skipping" in caplog.text


async def test_deferred_count_is_logged_per_poll(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        return PipelineOutcome("submitted", 1)  # pragma: no cover

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    far_1 = _question("1", close_time=NOW + timedelta(days=10))
    far_2 = _question("2", close_time=NOW + timedelta(days=15))
    deps = _deps(FakeMetaculus(questions=[far_1, far_2]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    caplog.set_level(logging.INFO, logger=daemon_module.__name__)
    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event, now=lambda: NOW, install_signal_handlers=False
            ),
        ),
        timeout=5,
    )
    await stopper

    assert "2 open question(s) deferred" in caplog.text
    # No probabilities or forecast content in the deferred-count log line.
    assert "%" not in caplog.text


async def test_deferred_question_is_claimed_once_it_enters_the_late_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression test for the 'no state needed' design: a question deferred
    on one poll needs no bookkeeping to be claimed once a later poll finds
    it inside the late window."""
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    close_time = NOW
    question = _question("1", close_time=close_time)
    deps = _deps(FakeMetaculus(questions=[question]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    # Poll 1: 200 minutes before close -- outside the default 180-minute
    # window. Poll 2 onward: 100 minutes before close -- inside it.
    poll_count = {"n": 0}

    def clock() -> datetime:
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return close_time - timedelta(minutes=200)
        return close_time - timedelta(minutes=100)

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        stop_event.set()

    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=0.02,
            hooks=DaemonHooks(
                stop_event=stop_event,
                now=clock,
                on_question_done=on_done,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )

    assert calls == ["1"]
    assert poll_count["n"] >= 2


async def test_practice_question_is_never_claimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hatim's 2026-09-24 decision: no forecasts on [PRACTICE] questions, even
    inside the late window."""
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    practice = _question(
        "1",
        close_time=NOW + timedelta(minutes=30),
        question_text='[PRACTICE] What will the average "new forecasters per day" be?',
    )
    real = _question("2", close_time=NOW + timedelta(minutes=30))
    deps = _deps(FakeMetaculus(questions=[practice, real]), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    caplog.set_level(logging.INFO, logger=daemon_module.__name__)
    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event, now=lambda: NOW, install_signal_handlers=False
            ),
        ),
        timeout=5,
    )
    await stopper

    assert calls == ["2"]
    assert "1 practice question(s) skipped" in caplog.text
