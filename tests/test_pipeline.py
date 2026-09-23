"""Tests for the spec s6 deadline ladder, driven by a fake (virtual) clock.

`deps.clock` and `deps.sleep` are both backed by a `FakeClock` so every test
runs in real milliseconds regardless of how many fake minutes the deadline
ladder crosses. `deps.sleep` both advances the fake clock and yields to the
event loop (`await asyncio.sleep(0)`), which is what lets concurrently
running model-attempt tasks make progress against the same virtual time.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from forecasting_tools.data_models.questions import BinaryQuestion

from betomcat.ledger import Ledger
from betomcat.llm import LLMError, LLMResult
from betomcat.pipeline import PipelineDeps, run_pipeline
from betomcat.pool import DrawResult
from betomcat.research import FamilyClassification, ResearchResult

POOL_MODELS = ["model-a", "model-b"]


class FakeClock:
    """A controllable virtual clock: `sleep` advances it and releases events."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self._pending: list[tuple[datetime, asyncio.Event]] = []

    def release_at(self, when: datetime) -> asyncio.Event:
        """An Event that becomes set once `self.now` reaches `when`."""
        event = asyncio.Event()
        if self.now >= when:
            event.set()
        else:
            self._pending.append((when, event))
        return event

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        still_pending = []
        for when, event in self._pending:
            if self.now >= when:
                event.set()
            else:
                still_pending.append((when, event))
        self._pending = still_pending
        await asyncio.sleep(0)


@dataclass
class ScriptedLLM:
    """Stands in for `OpenRouterClient`: dispatches to a per-model coroutine."""

    scripts: dict[str, object]
    calls: list[str] = field(default_factory=list)

    async def complete(self, model: object, prompt: str, timeout: float) -> LLMResult:
        self.calls.append(model.id)  # type: ignore[attr-defined]
        return await self.scripts[model.id]()  # type: ignore[operator]


@dataclass
class FakeIW:
    """No family, no research -- the deadline ladder is what's under test."""

    async def classify_family(
        self, question: str, question_id: object, context: object
    ) -> FamilyClassification:
        return FamilyClassification()

    async def research(self, **kwargs: object) -> ResearchResult:
        return ResearchResult(as_of="2026-09-22T00:00:00Z", briefing_text="(none)")


@dataclass
class FakeMetaculus:
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def post_binary(self, question: object, probability: float) -> None:
        self.calls.append(("post_binary", probability))

    async def post_multiple_choice(self, question: object, options: dict) -> None:
        self.calls.append(("post_multiple_choice", options))

    async def post_numeric(self, question: object, cdf_values: list) -> None:
        self.calls.append(("post_numeric", cdf_values))

    async def post_comment(self, question: object, text: str) -> None:
        self.calls.append(("post_comment", text))


def _write_pool(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "ensemble_width": 2,
                "models": [
                    {"id": "model-a", "tier": "frontier", "enabled": True},
                    {"id": "model-b", "tier": "frontier", "enabled": True},
                ],
            }
        )
    )
    return path


def _question(close_time: datetime) -> BinaryQuestion:
    return BinaryQuestion(
        question_text="Will X happen?",
        id_of_post=123,
        id_of_question=123,
        resolution_criteria="Resolves YES if X.",
        fine_print="",
        background_info="",
        close_time=close_time,
        scheduled_resolution_time=close_time,
        already_forecasted=False,
    )


def _deps(
    tmp_path: Path,
    llm: ScriptedLLM,
    clock: FakeClock,
    metaculus: FakeMetaculus,
    ledger: Ledger,
) -> PipelineDeps:
    return PipelineDeps(
        iw=FakeIW(),
        llm=llm,  # type: ignore[arg-type]
        metaculus=metaculus,  # type: ignore[arg-type]
        ledger=ledger,
        pool_path=_write_pool(tmp_path),
        data_dir=tmp_path,
        clock=lambda: clock.now,
        sleep=clock.sleep,
        rng=random.Random(0),
        soft_threshold=timedelta(seconds=10),
        hard_threshold=timedelta(seconds=5),
        poll_interval=1.0,
        retry_backoff=1.0,
        max_retries=2,
    )


async def _ok(text: str) -> LLMResult:
    return LLMResult(text=text, cost_usd=0.01, tokens_in=10, tokens_out=5)


async def test_second_model_slow_then_provisional_then_final(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)  # soft at +30s, hard at +35s
    clock = FakeClock(now)
    release_b = clock.release_at(now + timedelta(seconds=3))

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        await release_b.wait()
        return await _ok("Probability: 40%")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        submissions = ledger.get_submissions(outcome.run_id)
        assert [s["kind"] for s in submissions] == ["provisional", "final"]
        post_binary_calls = [c for c in metaculus.calls if c[0] == "post_binary"]
        assert len(post_binary_calls) == 2
    finally:
        ledger.close()


async def test_second_model_never_returns_provisional_stands(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        await asyncio.Event().wait()  # never set: this model never returns
        raise AssertionError("unreachable")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "provisional"
        submissions = ledger.get_submissions(outcome.run_id)
        assert [s["kind"] for s in submissions] == ["provisional"]
        post_binary_calls = [c for c in metaculus.calls if c[0] == "post_binary"]
        assert len(post_binary_calls) == 1
    finally:
        ledger.close()


async def test_nothing_by_hard_is_missed_with_no_submission(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    async def hang() -> LLMResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    llm = ScriptedLLM({"model-a": hang, "model-b": hang})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "missed"
        assert ledger.get_submissions(outcome.run_id) == []
        assert metaculus.calls == []
    finally:
        ledger.close()


async def test_retry_before_soft_fires(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)
    attempts = {"count": 0}

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise LLMError("transient failure")
        return await _ok("Probability: 40%")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        assert attempts["count"] == 2
        forecasts_b = [
            f
            for f in ledger.get_model_forecasts(outcome.run_id)
            if f["model_id"] == "model-b"
        ]
        assert [f["status"] for f in forecasts_b] == ["failed", "ok"]
        assert [f["attempt"] for f in forecasts_b] == [1, 2]
    finally:
        ledger.close()


async def test_draw_fallback_is_recorded_in_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    fake_result = DrawResult(
        models=["model-a", "model-b"],
        weights={"model-a": 5.0, "model-b": 4.0},
        pool_avg=3.3333,
        fallback_fired=True,
    )
    monkeypatch.setattr(
        "betomcat.pipeline.draw_pool", lambda pool, weights, rng: fake_result
    )

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        return await _ok("Probability: 40%")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        fallbacks = ledger.get_fallbacks(outcome.run_id)
        assert len(fallbacks) == 1
        assert fallbacks[0]["first_model"] == "model-a"
    finally:
        ledger.close()


async def test_already_forecasted_question_is_skipped(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)
    question = _question(close_time)
    question.already_forecasted = True

    llm = ScriptedLLM({})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(question, deps)

        assert outcome.status == "skipped"
        assert metaculus.calls == []
    finally:
        ledger.close()
