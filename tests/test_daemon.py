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


# -- concurrency cap ------------------------------------------------------------


def test_max_concurrent_questions_defaults_to_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAX_CONCURRENT_QUESTIONS", raising=False)

    assert daemon_module._max_concurrent_questions() == 4


def test_max_concurrent_questions_reads_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "7")

    assert daemon_module._max_concurrent_questions() == 7


@pytest.mark.parametrize("raw", ["0", "-2"])
def test_max_concurrent_questions_rejects_a_cap_below_one(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", raw)

    with pytest.raises(ValueError, match="MAX_CONCURRENT_QUESTIONS"):
        daemon_module._max_concurrent_questions()


async def test_daemon_never_exceeds_the_cap_and_finishes_every_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """poll_seconds is an hour, so all six questions only finish if a freed
    slot triggers an early re-poll."""
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "2")
    running = {"now": 0, "peak": 0}
    started: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        started.append(question.id_of_question)
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    questions = [_question(str(n)) for n in range(6)]
    deps = _deps(FakeMetaculus(questions=questions), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()
    done: list[str] = []

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        done.append(question_id)
        if len(done) == len(questions):
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

    assert running["peak"] == 2
    assert sorted(started) == ["0", "1", "2", "3", "4", "5"]


async def test_freed_slot_is_refilled_without_waiting_a_full_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "1")
    started: dict[str, asyncio.Event] = {"1": asyncio.Event(), "2": asyncio.Event()}
    release_first = asyncio.Event()

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        started[question.id_of_question].set()
        if question.id_of_question == "1":
            await release_first.wait()
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(
        FakeMetaculus(questions=[_question("1"), _question("2")]),
        FakeLedger(),
        tmp_path,
    )
    stop_event = asyncio.Event()
    daemon_task = asyncio.create_task(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(stop_event=stop_event, install_signal_handlers=False),
        )
    )

    await asyncio.wait_for(started["1"].wait(), timeout=5)
    await asyncio.sleep(0.05)
    assert not started["2"].is_set()

    release_first.set()
    await asyncio.wait_for(started["2"].wait(), timeout=5)

    stop_event.set()
    await asyncio.wait_for(daemon_task, timeout=5)


async def test_a_question_in_flight_is_never_claimed_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FakeLedger.has_run stays false throughout, like the window before a
    pipeline has written its run row, and a tiny poll interval re-polls
    constantly while question 1 is still running."""
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "4")
    calls: list[str] = []
    release_first = asyncio.Event()

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        if question.id_of_question == "1":
            await release_first.wait()
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(
        FakeMetaculus(questions=[_question("1"), _question("2")]),
        FakeLedger(),
        tmp_path,
    )
    stop_event = asyncio.Event()
    daemon_task = asyncio.create_task(
        run_daemon(
            (1,),
            deps,
            poll_seconds=0.01,
            hooks=DaemonHooks(stop_event=stop_event, install_signal_handlers=False),
        )
    )

    await asyncio.sleep(0.2)
    release_first.set()
    await asyncio.sleep(0.1)
    stop_event.set()
    await asyncio.wait_for(daemon_task, timeout=5)

    assert sorted(calls) == ["1", "2"]


async def test_a_skipped_question_does_not_cause_a_repoll_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pipeline that finishes at once without writing a run row (an
    unsupported question type) must not be re-claimed on every wake."""
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "1")
    calls: list[str] = []

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        calls.append(question.id_of_question)
        return PipelineOutcome("skipped", None, "unsupported")

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    deps = _deps(
        FakeMetaculus(questions=[_question("1"), _question("2")]),
        FakeLedger(),
        tmp_path,
    )
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.2)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
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

    assert sorted(calls) == ["1", "2"]


async def test_should_claim_is_called_once_per_actual_claim_under_the_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Questions waiting for a slot, already forecast, or already run must not
    consume a `should_claim` call (a --max-questions counter would drain)."""
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "2")
    claims: list[str] = []
    should_claim_calls = {"n": 0}

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        claims.append(question.id_of_question)
        await asyncio.sleep(0.02)
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    questions = [_question(str(n)) for n in range(1, 7)]
    metaculus = FakeMetaculus(questions=questions, forecasted={"5"})
    ledger = FakeLedger(runs={"metaculus:6"})
    deps = _deps(metaculus, ledger, tmp_path)
    stop_event = asyncio.Event()

    def should_claim() -> bool:
        should_claim_calls["n"] += 1
        return True

    async def on_done(question_id: str, outcome: PipelineOutcome) -> None:
        if len(claims) == 4 and question_id == f"metaculus:{claims[-1]}":
            stop_event.set()

    await asyncio.wait_for(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event,
                should_claim=should_claim,
                on_question_done=on_done,
                install_signal_handlers=False,
            ),
        ),
        timeout=5,
    )

    assert sorted(claims) == ["1", "2", "3", "4"]
    assert should_claim_calls["n"] == len(claims)


async def test_heartbeat_reports_the_cap_and_questions_waiting_for_a_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_QUESTIONS", "1")
    release = asyncio.Event()

    async def fake_run_pipeline(question: Any, deps: PipelineDeps) -> PipelineOutcome:
        await release.wait()
        return PipelineOutcome("submitted", 1)

    monkeypatch.setattr(daemon_module, "run_pipeline", fake_run_pipeline)

    questions = [_question(str(n)) for n in range(1, 4)]
    deps = _deps(FakeMetaculus(questions=questions), FakeLedger(), tmp_path)
    stop_event = asyncio.Event()
    caplog.set_level(logging.INFO, logger=daemon_module.__name__)
    daemon_task = asyncio.create_task(
        run_daemon(
            (1,),
            deps,
            poll_seconds=3600,
            hooks=DaemonHooks(
                stop_event=stop_event, drain_timeout=0.05, install_signal_handlers=False
            ),
        )
    )

    await asyncio.sleep(0.1)
    stop_event.set()
    await asyncio.wait_for(daemon_task, timeout=5)

    assert "1 question(s) in flight (cap 1)" in caplog.text
    assert "2 waiting for a slot" in caplog.text
    assert "%" not in caplog.text
