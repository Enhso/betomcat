"""Tests for `run_daemon`'s host-mode hooks (`DaemonHooks`)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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


def _question(qid: str) -> SimpleNamespace:
    return SimpleNamespace(id_of_question=qid)


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
