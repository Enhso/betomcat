"""Tests for the spec s6 deadline ladder, driven by a fake (virtual) clock.

`deps.clock` and `deps.sleep` are both backed by a `FakeClock` so every test
runs in real milliseconds regardless of how many fake minutes the deadline
ladder crosses. `deps.sleep` both advances the fake clock and yields to the
event loop (`await asyncio.sleep(0)`), which is what lets concurrently
running model-attempt tasks make progress against the same virtual time.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import orjson
import pytest
import yaml
from forecasting_tools.data_models.questions import BinaryQuestion
from pytest_httpx import HTTPXMock

from betomcat.budget import PacingResult
from betomcat.forecast import ModelResult
from betomcat.ledger import Ledger
from betomcat.llm import LLMError, LLMResult
from betomcat.pipeline import (
    FREE_NEWS_PROVIDERS,
    PipelineDeps,
    PipelineOutcome,
    _diverged,
    asknews_cycle_start,
    run_pipeline,
)
from betomcat.pool import DrawResult, ModelSpec, write_weights
from betomcat.rationale import RATIONALE_MODEL
from betomcat.referee import JEV_URL, REFEREE_MODEL, JevClient
from betomcat.research import FamilyClassification, IWClient, ResearchResult

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
    """Stands in for `OpenRouterClient`: dispatches to a per-model coroutine.

    Rationale-synthesis calls (`RATIONALE_MODEL`) are kept apart from the
    forecast calls: they go to `rationale` (default: fail, so the fallback
    comment is posted) and are logged in `rationale_calls` as `(prompt,
    timeout)`, never in `calls`. The referee's query writer (`REFEREE_MODEL`,
    the same Luna id) goes to `referee` (default: fail) and its prompts land in
    `referee_prompts`. Forecast prompts are kept in `prompts` as `(model id,
    prompt)`, in call order.
    """

    scripts: dict[str, object]
    rationale: Callable[[], Awaitable[LLMResult]] | None = None
    calls: list[str] = field(default_factory=list)
    rationale_calls: list[tuple[str, float]] = field(default_factory=list)
    referee: Callable[[], Awaitable[LLMResult]] | None = None
    referee_prompts: list[str] = field(default_factory=list)
    prompts: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, model: object, prompt: str, timeout: float) -> LLMResult:
        if model == REFEREE_MODEL:
            self.referee_prompts.append(prompt)
            if self.referee is None:
                raise LLMError("no referee script")
            return await self.referee()
        if model.id == RATIONALE_MODEL.id:  # type: ignore[attr-defined]
            self.rationale_calls.append((prompt, timeout))
            if self.rationale is None:
                raise LLMError("no rationale script")
            return await self.rationale()
        self.calls.append(model.id)  # type: ignore[attr-defined]
        self.prompts.append((model.id, prompt))  # type: ignore[attr-defined]
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


def _write_pool_three(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "ensemble_width": 2,
                "models": [
                    {"id": "model-a", "tier": "frontier", "enabled": True},
                    {"id": "model-b", "tier": "frontier", "enabled": True},
                    {"id": "model-c", "tier": "frontier", "enabled": True},
                ],
            }
        )
    )
    return path


@dataclass
class FakeBudgetGuard:
    """Always excludes `excluded_id`, standing in for a live pacing verdict."""

    excluded_id: str
    pace: float = 1.5
    daily_budget: float = 8.0

    async def restrict(
        self,
        models: list[ModelSpec],
        prompt_chars: int,
        ledger: Ledger,
        now: object,
    ) -> PacingResult:
        eligible = [m for m in models if m.id != self.excluded_id]
        return PacingResult(
            eligible=eligible,
            pace=self.pace,
            daily_budget=self.daily_budget,
            excluded_ids=[self.excluded_id],
        )


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
        assert [s["comment_posted"] for s in submissions] == [0, 1]
        post_binary_calls = [c for c in metaculus.calls if c[0] == "post_binary"]
        assert len(post_binary_calls) == 2
        comment_calls = [c for c in metaculus.calls if c[0] == "post_comment"]
        assert len(comment_calls) == 1
        assert "final forecast" in str(comment_calls[0][1])
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
    comment_times: list[datetime] = []
    post_comment = metaculus.post_comment

    async def timed_post_comment(question: object, text: str) -> None:
        comment_times.append(clock.now)
        await post_comment(question, text)

    metaculus.post_comment = timed_post_comment  # type: ignore[method-assign]
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "provisional"
        submissions = ledger.get_submissions(outcome.run_id)
        assert [s["kind"] for s in submissions] == ["provisional"]
        assert submissions[0]["comment_posted"] == 1
        post_binary_calls = [c for c in metaculus.calls if c[0] == "post_binary"]
        assert len(post_binary_calls) == 1
        comment_calls = [c for c in metaculus.calls if c[0] == "post_comment"]
        assert len(comment_calls) == 1
        assert "provisional forecast" in str(comment_calls[0][1])
        assert comment_times[0] >= close_time - deps.hard_threshold
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


async def test_budget_pacing_narrows_pool_before_draw(tmp_path: Path) -> None:
    """A model excluded by the budget guard is never drawn, called, or
    reconciled, and the exclusion is recorded in the ledger and comment."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        return await _ok("Probability: 40%")

    async def script_c() -> LLMResult:
        raise AssertionError("model-c was excluded by pacing and must not be called")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b, "model-c": script_c})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        deps.pool_path = _write_pool_three(tmp_path)
        deps.budget = FakeBudgetGuard(excluded_id="model-c")  # type: ignore[assignment]

        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        assert "model-c" not in llm.calls
        assert set(llm.calls) == {"model-a", "model-b"}

        pacing_decisions = ledger.get_pacing_decisions(outcome.run_id)
        assert len(pacing_decisions) == 1
        assert pacing_decisions[0]["pace"] == pytest.approx(1.5)
        assert orjson.loads(pacing_decisions[0]["excluded_ids"]) == ["model-c"]

        comment_calls = [c for c in metaculus.calls if c[0] == "post_comment"]
        assert comment_calls
        # The pacing exclusion is on the audit report, not the short posted comment.
        assert "budget pacing: excluded model-c" not in comment_calls[-1][1]
        submissions = ledger.get_submissions(outcome.run_id)
        assert "budget pacing: excluded model-c" in submissions[-1]["report"]
    finally:
        ledger.close()


# -- replacement (a drawn model that gives up before soft gets a spare) -----


async def test_failed_slot_is_replaced_by_spare_and_final_uses_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """model-c is drawn but fails every attempt; before soft it's replaced by
    the untried spare model-a, whose weight comes from the draw-time weights
    snapshot; the final submission reconciles the survivor (model-b) and the
    replacement (model-a), and the ledger has one draw row per model tried."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    fake_result = DrawResult(
        models=["model-c", "model-b"],
        weights={"model-c": 1.0, "model-b": 1.0},
        pool_avg=1.0,
        fallback_fired=False,
    )
    monkeypatch.setattr(
        "betomcat.pipeline.draw_pool", lambda pool, weights, rng: fake_result
    )

    async def script_a() -> LLMResult:
        return await _ok("Probability: 55%")

    async def script_b() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_c() -> LLMResult:
        raise LLMError("model-c is down")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b, "model-c": script_c})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        deps.pool_path = _write_pool_three(tmp_path)
        # The replacement's weight must come from this snapshot, taken at
        # draw time -- not a value re-read later.
        write_weights(
            tmp_path,
            weights={"model-a": 7.0},
            n_resolved={},
            updated_at="2026-09-22T00:00:00Z",
        )

        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        submissions = ledger.get_submissions(outcome.run_id)
        # model-b succeeds immediately (provisional); model-c fails and is
        # replaced by model-a, whose success completes the final.
        assert [s["kind"] for s in submissions] == ["provisional", "final"]

        draws = ledger.get_draws(outcome.run_id)
        assert {d["model_id"] for d in draws} == {"model-a", "model-b", "model-c"}
        assert len(draws) == 3
        replacement_row = next(d for d in draws if d["model_id"] == "model-a")
        assert replacement_row["weight"] == pytest.approx(7.0)

        # Final reconciliation is the weighted average of model-b (0.65,
        # weight 1.0) and model-a (0.55, weight 7.0 from the snapshot) --
        # confirms the replacement's snapshot weight was actually used.
        post_binary_calls = [c for c in metaculus.calls if c[0] == "post_binary"]
        assert len(post_binary_calls) == 2
        expected_final = (0.65 * 1.0 + 0.55 * 7.0) / 8.0
        assert post_binary_calls[-1][1] == pytest.approx(expected_final)
    finally:
        ledger.close()


async def test_replacement_stops_at_max_replacements_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the cap set to 1, only the first failed slot gets replaced even
    though a second slot also fails and a second spare remains untried."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)
    monkeypatch.setattr("betomcat.pipeline.MAX_REPLACEMENTS_PER_RUN", 1)

    fake_result = DrawResult(
        models=["model-c", "model-d"],
        weights={"model-c": 1.0, "model-d": 1.0},
        pool_avg=1.0,
        fallback_fired=False,
    )
    monkeypatch.setattr(
        "betomcat.pipeline.draw_pool", lambda pool, weights, rng: fake_result
    )

    async def fail() -> LLMResult:
        raise LLMError("down")

    async def script_ok() -> LLMResult:
        return await _ok("Probability: 60%")

    llm = ScriptedLLM(
        {
            "model-a": script_ok,
            "model-b": script_ok,
            "model-c": fail,
            "model-d": fail,
        }
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        # _deps's own _write_pool call already wrote this same path with its
        # default 2-model pool; overwrite it here with the 4-model pool.
        path = tmp_path / "models.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "ensemble_width": 2,
                    "models": [
                        {"id": "model-a", "tier": "frontier", "enabled": True},
                        {"id": "model-b", "tier": "frontier", "enabled": True},
                        {"id": "model-c", "tier": "frontier", "enabled": True},
                        {"id": "model-d", "tier": "frontier", "enabled": True},
                    ],
                }
            )
        )
        deps.pool_path = path

        outcome = await run_pipeline(_question(close_time), deps)

        # Only one of the two failed slots got a replacement; the other
        # never produces a result, so the run never reaches "all slots done".
        assert outcome.status == "provisional"
        draws = ledger.get_draws(outcome.run_id)
        assert len(draws) == 3
        assert {d["model_id"] for d in draws} == {"model-c", "model-d", "model-b"}
        assert "model-a" not in llm.calls
    finally:
        ledger.close()


async def test_no_replacement_after_soft_deadline(tmp_path: Path) -> None:
    """A slot model that only fails once the clock has already crossed soft
    gets the existing duplicate-attempt path, never a replacement -- the
    spare is left untouched.

    With `_write_pool_three` and the default seeded rng (unmonkeypatched
    draw), the real draw picks model-c and model-b, leaving model-a as the
    untried spare (verified directly against `pool.draw`)."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)  # soft at +30s, hard at +35s
    clock = FakeClock(now)
    release_b = clock.release_at(now + timedelta(seconds=31))

    async def script_c() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        await release_b.wait()
        raise LLMError("down, and only fails once past soft")

    async def script_a() -> LLMResult:
        raise AssertionError("model-a is a spare and must never be called")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b, "model-c": script_c})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        deps.pool_path = _write_pool_three(tmp_path)

        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "provisional"
        assert "model-a" not in llm.calls
        draws = ledger.get_draws(outcome.run_id)
        assert {d["model_id"] for d in draws} == {"model-c", "model-b"}
    finally:
        ledger.close()


async def test_no_spares_behaves_exactly_as_before(tmp_path: Path) -> None:
    """With a 2-model pool at width 2 there are no spares; a failing model
    gets only the pre-existing retry/duplicate handling, and the provisional
    result stands -- unchanged from the pre-replacement behaviour."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        raise LLMError("model-b is always down")

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b})
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)  # _write_pool: a, b only

        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "provisional"
        draws = ledger.get_draws(outcome.run_id)
        assert {d["model_id"] for d in draws} == {"model-a", "model-b"}
        submissions = ledger.get_submissions(outcome.run_id)
        assert [s["kind"] for s in submissions] == ["provisional"]
    finally:
        ledger.close()


# -- the synthesized comment (one rationale paragraph, fallback on failure) --

RATIONALE_REPLY = " ".join(f"rationale{i}" for i in range(60))
RATIONALE_A = "Model A saw the 2026-09-01 vote slip. Probability: 65%"
RATIONALE_B = "Model B anchored on a 30% base rate. Probability: 40%"


def _comment_texts(metaculus: FakeMetaculus) -> list[str]:
    return [str(c[1]) for c in metaculus.calls if c[0] == "post_comment"]


async def _rationale_reply() -> LLMResult:
    return await _ok(RATIONALE_REPLY)


async def test_final_posts_the_synthesized_comment_once(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)  # soft at +30s, hard at +35s
    clock = FakeClock(now)
    release_b = clock.release_at(now + timedelta(seconds=3))

    async def script_a() -> LLMResult:
        return await _ok(RATIONALE_A)

    async def script_b() -> LLMResult:
        await release_b.wait()
        return await _ok(RATIONALE_B)

    llm = ScriptedLLM(
        {"model-a": script_a, "model-b": script_b}, rationale=_rationale_reply
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        assert [s["kind"] for s in ledger.get_submissions(outcome.run_id)] == [
            "provisional",
            "final",
        ]
        comments = _comment_texts(metaculus)
        assert comments == [f"betomcat v1.0.0, final forecast\n\n{RATIONALE_REPLY}"]
        # Only the final (not the earlier provisional) triggers synthesis.
        assert len(llm.rationale_calls) == 1
        prompt, timeout = llm.rationale_calls[0]
        assert RATIONALE_A in prompt
        assert RATIONALE_B in prompt
        assert "52.5% probability of YES" in prompt  # the reconciled 0.65/0.40 mean
        assert 0 < timeout <= 35.0  # never past hard (+35s)
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "failing_reply",
    [
        pytest.param(LLMError("luna is down"), id="llm-error"),
        pytest.param("too short to use", id="short-reply"),
    ],
)
async def test_final_falls_back_to_the_summary_comment(
    tmp_path: Path, failing_reply: LLMError | str
) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)
    clock = FakeClock(now)

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        return await _ok("Probability: 40%")

    async def rationale() -> LLMResult:
        if isinstance(failing_reply, LLMError):
            raise failing_reply
        return await _ok(failing_reply)

    llm = ScriptedLLM({"model-a": script_a, "model-b": script_b}, rationale=rationale)
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        outcome = await run_pipeline(_question(close_time), deps)

        assert outcome.status == "submitted"
        comments = _comment_texts(metaculus)
        assert len(comments) == 1
        assert comments[0].startswith("betomcat v1.0.0, final forecast\n- **model-")
        assert "too short to use" not in comments[0]
        submissions = ledger.get_submissions(outcome.run_id)
        assert [s["comment_posted"] for s in submissions] == [1]
    finally:
        ledger.close()


async def test_final_skips_synthesis_when_hard_is_already_reached(
    tmp_path: Path,
) -> None:
    """Results landing at or past hard get the fallback with no synthesis call."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)  # hard at +35s
    clock = FakeClock(now)

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def script_b() -> LLMResult:
        clock.now = now + timedelta(seconds=36)
        return await _ok("Probability: 40%")

    llm = ScriptedLLM(
        {"model-a": script_a, "model-b": script_b}, rationale=_rationale_reply
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        await run_pipeline(_question(close_time), deps)

        assert llm.rationale_calls == []
        comments = _comment_texts(metaculus)
        assert len(comments) == 1
        assert "- **model-" in comments[0]
    finally:
        ledger.close()


async def _run_provisional_standing_at_hard(
    tmp_path: Path, llm: ScriptedLLM, margin: timedelta
) -> tuple[FakeMetaculus, datetime, list[datetime]]:
    """model-a answers; model-b never does, so the provisional stands at hard."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    close_time = now + timedelta(seconds=40)  # hard at +35s
    clock = FakeClock(now)
    metaculus = FakeMetaculus()
    comment_times: list[datetime] = []
    post_comment = metaculus.post_comment

    async def timed_post_comment(question: object, text: str) -> None:
        comment_times.append(clock.now)
        await post_comment(question, text)

    metaculus.post_comment = timed_post_comment  # type: ignore[method-assign]
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _deps(tmp_path, llm, clock, metaculus, ledger)
        deps.rationale_close_margin = margin
        outcome = await run_pipeline(_question(close_time), deps)
        assert outcome.status == "provisional"
        assert ledger.get_submissions(outcome.run_id)[0]["comment_posted"] == 1
    finally:
        ledger.close()
    return metaculus, close_time, comment_times


async def _never() -> LLMResult:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def test_provisional_standing_at_hard_posts_the_synthesized_comment(
    tmp_path: Path,
) -> None:
    async def script_a() -> LLMResult:
        return await _ok(RATIONALE_A)

    llm = ScriptedLLM(
        {"model-a": script_a, "model-b": _never}, rationale=_rationale_reply
    )

    metaculus, close_time, comment_times = await _run_provisional_standing_at_hard(
        tmp_path, llm, margin=timedelta(seconds=2)
    )

    comments = _comment_texts(metaculus)
    assert comments == [f"betomcat v1.0.0, provisional forecast\n\n{RATIONALE_REPLY}"]
    assert len(llm.rationale_calls) == 1
    prompt, timeout = llm.rationale_calls[0]
    assert RATIONALE_A in prompt
    assert "65% probability of YES" in prompt
    # Synthesis runs at hard (+35s), and ends by close minus the margin (+38s).
    assert 0 < timeout <= 3.0
    assert comment_times[0] >= close_time - timedelta(seconds=5)


async def test_provisional_at_hard_with_no_time_before_close_posts_fallback(
    tmp_path: Path,
) -> None:
    """The default 60 s margin leaves no room in this 5 s hard-to-close window."""

    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    llm = ScriptedLLM(
        {"model-a": script_a, "model-b": _never}, rationale=_rationale_reply
    )

    metaculus, _close_time, _times = await _run_provisional_standing_at_hard(
        tmp_path, llm, margin=timedelta(seconds=60)
    )

    assert llm.rationale_calls == []
    comments = _comment_texts(metaculus)
    assert len(comments) == 1
    assert comments[0].startswith(
        "betomcat v1.0.0, provisional forecast\n- **model-a**"
    )


async def test_provisional_at_hard_synthesis_failure_posts_fallback(
    tmp_path: Path,
) -> None:
    async def script_a() -> LLMResult:
        return await _ok("Probability: 65%")

    async def rationale() -> LLMResult:
        raise LLMError("luna is down")

    llm = ScriptedLLM({"model-a": script_a, "model-b": _never}, rationale=rationale)

    metaculus, _close_time, _times = await _run_provisional_standing_at_hard(
        tmp_path, llm, margin=timedelta(seconds=2)
    )

    comments = _comment_texts(metaculus)
    assert len(comments) == 1
    assert comments[0].startswith(
        "betomcat v1.0.0, provisional forecast\n- **model-a**"
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(
            datetime(2026, 10, 15, 9, 30, tzinfo=UTC),
            datetime(2026, 9, 22, tzinfo=UTC),
            id="before-the-22nd-is-the-previous-months-22nd",
        ),
        pytest.param(
            datetime(2026, 10, 21, 23, 59, 59, tzinfo=UTC),
            datetime(2026, 9, 22, tzinfo=UTC),
            id="last-second-before-the-cycle",
        ),
        pytest.param(
            datetime(2026, 10, 22, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 22, tzinfo=UTC),
            id="the-22nd-at-midnight-maps-to-itself",
        ),
        pytest.param(
            datetime(2026, 10, 31, 18, 45, tzinfo=UTC),
            datetime(2026, 10, 22, tzinfo=UTC),
            id="after-the-22nd-is-this-months-22nd",
        ),
        pytest.param(
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
            datetime(2026, 12, 22, tzinfo=UTC),
            id="end-of-december",
        ),
        pytest.param(
            datetime(2027, 1, 15, 12, 0, tzinfo=UTC),
            datetime(2026, 12, 22, tzinfo=UTC),
            id="15-january-is-22-december-of-the-previous-year",
        ),
        pytest.param(
            datetime(2027, 1, 22, 0, 0, tzinfo=UTC),
            datetime(2027, 1, 22, tzinfo=UTC),
            id="22-january-maps-to-itself",
        ),
        pytest.param(
            datetime(2026, 10, 22, 1, 0, tzinfo=timezone(timedelta(hours=2))),
            datetime(2026, 9, 22, tzinfo=UTC),
            id="offset-time-is-read-in-utc",
        ),
    ],
)
def test_asknews_cycle_start(now: datetime, expected: datetime) -> None:
    assert asknews_cycle_start(now) == expected


CYCLE_START = "2026-10-22T00:00:00Z"
IW_URL = "http://iw.test"
ASKNEWS_REQUEST = re.compile(r"https://api\.asknews\.app/.*")
GOOGLE_REQUEST = re.compile(r"https://news\.google\.com/rss/search.*")
GOOGLE_FIXTURE = Path(__file__).parent / "fixtures" / "google_news_rss.xml"
IW_RESEARCH_BODY = {
    "dossier_id": "dos:budget",
    "as_of": "2026-10-23T12:00:00Z",
    "briefing": {"situation_summary": "Calm markets."},
    "claims": [],
    "family_claims": [],
    "history": [],
    "gate_log": [],
}


@pytest.fixture
def asknews_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A fake clock that the ledger also stamps its rows with.

    The pipeline reads the cycle from `deps.clock` but the ledger stamps
    `recorded_at` from its own clock, so both must agree for calls recorded
    by one run to count against the next.
    """
    clock = FakeClock(datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(
        "betomcat.ledger._now_iso",
        lambda: clock.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return clock


@pytest.fixture
def budget_ledger(tmp_path: Path) -> Iterator[Ledger]:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


async def _says_65() -> LLMResult:
    return await _ok("Probability: 65%")


def _budget_deps(
    tmp_path: Path, clock: FakeClock, ledger: Ledger, **overrides: object
) -> PipelineDeps:
    """Pipeline deps with the real IW client, so what IW is asked is on the wire."""
    llm = ScriptedLLM({"model-a": _says_65, "model-b": _says_65})
    deps = _deps(tmp_path, llm, clock, FakeMetaculus(), ledger)
    return replace(deps, iw=IWClient(IW_URL, "ank_test", tmp_path), **overrides)


def _seed_asknews_calls(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch, calls: int, recorded_at: str
) -> int:
    """Record `calls` AskNews calls as of `recorded_at`; return the seeding run id."""
    ledger.upsert_question("seed:1", "Seed?", "binary", None, None)
    run = ledger.start_run("seed:1")
    with monkeypatch.context() as patch:
        patch.setattr("betomcat.ledger._now_iso", lambda: recorded_at)
        ledger.record_asknews_calls(run.id, calls)
    return run.id


async def _run_question(
    deps: PipelineDeps, clock: FakeClock, question_id: int
) -> PipelineOutcome:
    question = _question(clock.now + timedelta(seconds=40)).model_copy(
        update={"id_of_question": question_id, "id_of_post": question_id}
    )
    return await run_pipeline(question, deps)


def _mock_iw_up(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{IW_URL}/api/families/classify",
        json={"decision": "none", "method": "jev"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=f"{IW_URL}/api/research", json=IW_RESEARCH_BODY, is_reusable=True
    )


def _mock_iw_research_down(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{IW_URL}/api/families/classify",
        json={"decision": "none", "method": "jev"},
    )
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)


def _iw_research_bodies(httpx_mock: HTTPXMock) -> list[dict[str, object]]:
    return [
        orjson.loads(request.content)
        for request in httpx_mock.get_requests(url=f"{IW_URL}/api/research")
    ]


def _asknews_rows(tmp_path: Path) -> list[tuple[int, int]]:
    connection = sqlite3.connect(tmp_path / "ledger.sqlite")
    try:
        rows = connection.execute("SELECT run_id, calls FROM asknews_calls ORDER BY id")
        return [(run_id, calls) for run_id, calls in rows]
    finally:
        connection.close()


async def test_research_that_just_fits_the_cap_keeps_asknews_and_records_its_calls(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    asknews_clock: FakeClock,
    budget_ledger: Ledger,
) -> None:
    seed_run = _seed_asknews_calls(budget_ledger, monkeypatch, 748, CYCLE_START)
    _mock_iw_up(httpx_mock)
    deps = _budget_deps(tmp_path, asknews_clock, budget_ledger)

    outcome = await _run_question(deps, asknews_clock, 201)

    assert outcome.status == "submitted"
    (body,) = _iw_research_bodies(httpx_mock)
    assert body["question"] == "Will X happen?"
    assert "providers" not in body
    assert _asknews_rows(tmp_path) == [(seed_run, 748), (outcome.run_id, 2)]
    assert budget_ledger.asknews_calls_since(CYCLE_START) == 750


async def test_research_that_would_exceed_the_cap_asks_iw_for_free_providers_only(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    asknews_clock: FakeClock,
    budget_ledger: Ledger,
) -> None:
    seed_run = _seed_asknews_calls(budget_ledger, monkeypatch, 749, CYCLE_START)
    _mock_iw_up(httpx_mock)
    deps = _budget_deps(tmp_path, asknews_clock, budget_ledger)

    outcome = await _run_question(deps, asknews_clock, 201)

    assert outcome.status == "submitted"
    (body,) = _iw_research_bodies(httpx_mock)
    assert body["providers"] == ["google_news", "bing_news", "wikipedia"]
    assert _asknews_rows(tmp_path) == [(seed_run, 749)]
    assert budget_ledger.asknews_calls_since(CYCLE_START) == 749


async def test_calls_are_recorded_per_run_until_the_cap_is_spent(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    asknews_clock: FakeClock,
    budget_ledger: Ledger,
) -> None:
    _mock_iw_up(httpx_mock)
    deps = _budget_deps(tmp_path, asknews_clock, budget_ledger, asknews_monthly_calls=4)

    outcomes = [
        await _run_question(deps, asknews_clock, qid) for qid in (201, 202, 203)
    ]

    assert [outcome.status for outcome in outcomes] == ["submitted"] * 3
    assert _asknews_rows(tmp_path) == [
        (outcomes[0].run_id, 2),
        (outcomes[1].run_id, 2),
    ]
    assert ["providers" in body for body in _iw_research_bodies(httpx_mock)] == [
        False,
        False,
        True,
    ]
    assert _iw_research_bodies(httpx_mock)[2]["providers"] == [
        "google_news",
        "bing_news",
        "wikipedia",
    ]
    assert budget_ledger.asknews_calls_since(CYCLE_START) == 4


async def test_calls_from_before_the_cycle_start_do_not_count(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    asknews_clock: FakeClock,
    budget_ledger: Ledger,
) -> None:
    asknews_clock.now = datetime(2026, 10, 22, 0, 0, 0, tzinfo=UTC)
    seed_run = _seed_asknews_calls(
        budget_ledger, monkeypatch, 750, "2026-10-21T23:59:59Z"
    )
    _mock_iw_up(httpx_mock)
    deps = _budget_deps(tmp_path, asknews_clock, budget_ledger)

    outcome = await _run_question(deps, asknews_clock, 201)

    (body,) = _iw_research_bodies(httpx_mock)
    assert "providers" not in body
    assert _asknews_rows(tmp_path) == [(seed_run, 750), (outcome.run_id, 2)]
    assert budget_ledger.asknews_calls_since(CYCLE_START) == 2


async def test_over_budget_with_iw_down_the_fallback_makes_no_asknews_request(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    asknews_clock: FakeClock,
    budget_ledger: Ledger,
) -> None:
    seed_run = _seed_asknews_calls(budget_ledger, monkeypatch, 749, CYCLE_START)
    _mock_iw_research_down(httpx_mock)
    httpx_mock.add_response(
        url=GOOGLE_REQUEST, text=GOOGLE_FIXTURE.read_text(encoding="utf-8")
    )
    deps = _budget_deps(tmp_path, asknews_clock, budget_ledger)

    outcome = await _run_question(deps, asknews_clock, 201)

    assert outcome.status == "submitted"
    assert httpx_mock.get_requests(url=ASKNEWS_REQUEST) == []
    assert len(httpx_mock.get_requests(url=GOOGLE_REQUEST)) == 1
    run = budget_ledger.get_run(outcome.run_id)
    assert run is not None
    assert run["degraded"] == 1
    assert _asknews_rows(tmp_path) == [(seed_run, 749)]


async def test_under_budget_with_iw_down_the_fallback_call_is_recorded_too(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    asknews_clock: FakeClock,
    budget_ledger: Ledger,
) -> None:
    _mock_iw_research_down(httpx_mock)
    httpx_mock.add_response(
        url=ASKNEWS_REQUEST,
        json={"as_dicts": [{"eng_title": "ECB signals caution"}]},
    )
    httpx_mock.add_response(
        url=GOOGLE_REQUEST, text=GOOGLE_FIXTURE.read_text(encoding="utf-8")
    )
    deps = _budget_deps(tmp_path, asknews_clock, budget_ledger)

    outcome = await _run_question(deps, asknews_clock, 201)

    assert outcome.status == "submitted"
    assert len(httpx_mock.get_requests(url=ASKNEWS_REQUEST)) == 1
    # IW's planned 2 calls plus the 1 the fallback made: over-counting is the
    # documented safe side when IW fails.
    assert _asknews_rows(tmp_path) == [(outcome.run_id, 3)]


# -- the referee gate (HANDOFF s5.1) ---------------------------------------------

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
HANG = object()
FIRST_A = "MARKER-A1 The vote slipped.\nSummary: A first.\nProbability: 65%"
FIRST_B = "MARKER-B1 A base rate applies.\nSummary: B first.\nProbability: 40%"
SECOND_A = "Second look A.\nSummary: A second.\nProbability: 60%"
SECOND_B = "Second look B.\nSummary: B second.\nProbability: 50%"
# model-a weighs 3, model-b 1: (0.65 * 3 + 0.40) / 4 and (0.60 * 3 + 0.50) / 4.
FIRST_AVERAGE = 0.5875
SECOND_AVERAGE = 0.575
NO_LEAK = ("MARKER-A1", "MARKER-B1", "A first", "B first", "vote slipped")


def _replies(*items: object) -> Callable[[], Awaitable[LLMResult]]:
    """A model script that answers its calls in order.

    Each item is text, an error, `HANG`, or `(event, text)` to answer once the
    event is set.
    """
    remaining = list(items)

    async def script() -> LLMResult:
        item = remaining.pop(0)
        if item is HANG:
            await asyncio.Event().wait()
        if isinstance(item, tuple):
            event, item = item
            await event.wait()
        if isinstance(item, Exception):
            raise item
        return await _ok(str(item))

    return script


def _luna(diagnosis: str, queries: list[str]) -> Callable[[], Awaitable[LLMResult]]:
    async def script() -> LLMResult:
        return await _ok(
            orjson.dumps({"diagnosis": diagnosis, "queries": queries}).decode()
        )

    return script


def _jev_answer(choice: str) -> dict[str, object]:
    return {"answers": {"disagreement": {"choice": choice, "confidence": 0.9}}}


def _iw_body(summary: str, dossier: str) -> dict[str, object]:
    return {
        "dossier_id": dossier,
        "as_of": "2026-09-22T12:00:00Z",
        "briefing": {"situation_summary": summary},
        "claims": [],
        "family_claims": [],
        "history": [],
        "gate_log": [],
    }


def _mock_iw(
    httpx_mock: HTTPXMock, *second: tuple[int, dict[str, object]], first: bool = True
) -> None:
    """IW classify, the first research, and any further researches `second`."""
    httpx_mock.add_response(
        url=f"{IW_URL}/api/families/classify",
        json={"decision": "none", "method": "jev"},
        is_reusable=True,
    )
    if first:
        httpx_mock.add_response(
            url=f"{IW_URL}/api/research", json=_iw_body("FIRST-BRIEFING", "dos:1")
        )
    for status, body in second:
        httpx_mock.add_response(
            url=f"{IW_URL}/api/research", status_code=status, json=body
        )


SECOND_RESEARCH = (200, _iw_body("SECOND-BRIEFING", "dos:2"))


def _mock_jev(httpx_mock: HTTPXMock, choice: str) -> None:
    httpx_mock.add_response(url=JEV_URL, method="POST", json=_jev_answer(choice))


def _referee_deps(
    tmp_path: Path,
    llm: ScriptedLLM,
    clock: FakeClock,
    metaculus: FakeMetaculus,
    ledger: Ledger,
    **overrides: object,
) -> PipelineDeps:
    write_weights(
        tmp_path,
        weights={"model-a": 3.0, "model-b": 1.0},
        n_resolved={},
        updated_at="2026-09-22T00:00:00Z",
    )
    deps = _deps(tmp_path, llm, clock, metaculus, ledger)
    wiring: dict[str, object] = {
        "iw": IWClient(IW_URL, None, tmp_path),
        "jev": JevClient("jev_test_key"),
    }
    return replace(deps, **{**wiring, **overrides})


async def _run_diverging(deps: PipelineDeps, clock: FakeClock) -> PipelineOutcome:
    return await run_pipeline(_question(clock.now + timedelta(seconds=40)), deps)


def _posted_binary(metaculus: FakeMetaculus) -> list[float]:
    return [float(c[1]) for c in metaculus.calls if c[0] == "post_binary"]  # type: ignore[arg-type]


def _submission_rows(
    ledger: Ledger, run_id: int | None
) -> list[tuple[str, int, float]]:
    assert run_id is not None
    return [
        (s["kind"], s["comment_posted"], float(s["forecast_json"]))
        for s in ledger.get_submissions(run_id)
    ]


def _referee_row(ledger: Ledger, run_id: int | None) -> tuple[object, ...]:
    assert run_id is not None
    row = ledger.get_referee(run_id)
    assert row is not None
    return (row["disagreement_class"], row["rerun"], row["outcome"])


def _two_model_llm(
    first_a: object = FIRST_A,
    first_b: object = FIRST_B,
    second_a: object = SECOND_A,
    second_b: object = SECOND_B,
    **kwargs: object,
) -> ScriptedLLM:
    return ScriptedLLM(
        {
            "model-a": _replies(first_a, second_a),
            "model-b": _replies(first_b, second_b),
        },
        **kwargs,  # type: ignore[arg-type]
    )


async def test_stale_information_reruns_both_models_and_the_final_is_their_average(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(
        referee=_luna(
            "The vote date moved.", ["vote date postponed", "  second   query "]
        )
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        # The weighted average of the first round goes in as a provisional, the
        # weighted average of the two re-forecasts (same frozen weights) as the final.
        assert _posted_binary(metaculus) == pytest.approx(
            [FIRST_AVERAGE, SECOND_AVERAGE]
        )
        assert _submission_rows(ledger, outcome.run_id) == [
            ("provisional", 0, pytest.approx(FIRST_AVERAGE)),
            ("final", 1, pytest.approx(SECOND_AVERAGE)),
        ]
        (comment,) = _comment_texts(metaculus)
        header, *bullets = comment.split("\n")
        assert header == "betomcat v1.0.0, final forecast"
        assert sorted(bullets) == [
            "- **model-a** (60.00%): A second.",
            "- **model-b** (50.00%): B second.",
        ]
        assert _referee_row(ledger, outcome.run_id) == (
            "stale_or_missing_information",
            1,
            "rerun_ok",
        )

        first_body, second_body = _iw_research_bodies(httpx_mock)
        assert "extra_queries" not in first_body
        assert second_body["extra_queries"] == ["vote date postponed", "second query"]
        assert _asknews_rows(tmp_path) == [(outcome.run_id, 2), (outcome.run_id, 4)]

        (jev_request,) = httpx_mock.get_requests(url=JEV_URL)
        jev_body = orjson.loads(jev_request.content)
        assert jev_request.headers["Authorization"] == "Bearer jev_test_key"
        assert jev_body["state"]["question"] == "Will X happen?"
        assert jev_body["state"]["resolution_criteria"] == "Resolves YES if X."
        assert sorted(jev_body["state"]["rationales"]) == [FIRST_A, FIRST_B]

        (luna_prompt,) = llm.referee_prompts
        assert FIRST_A in luna_prompt and FIRST_B in luna_prompt
        assert "stale or missing information" in luna_prompt
        assert llm.rationale_calls[0][0].count("Forecast ") == 2

        first_round, second_round = llm.prompts[:2], llm.prompts[2:]
        assert sorted(model for model, _ in second_round) == ["model-a", "model-b"]
        assert second_round[0][1] == second_round[1][1]
        for _, prompt in first_round:
            assert "FIRST-BRIEFING" in prompt and "Second round" not in prompt
        for _, prompt in second_round:
            assert "SECOND-BRIEFING" in prompt and "FIRST-BRIEFING" not in prompt
            assert (
                "Likely cause: The vote date moved." in prompt
                and "### Second round" in prompt
            )
            # Never the other model's rationale (herding), nor even a model's own.
            for leaked in NO_LEAK:
                assert leaked not in prompt

        rows = ledger.get_model_forecasts(outcome.run_id)
        assert sorted((r["model_id"], r["attempt"], r["status"]) for r in rows) == [
            ("model-a", -2, "ok"),
            ("model-a", 1, "ok"),
            ("model-b", -2, "ok"),
            ("model-b", 1, "ok"),
        ]
        last = {
            r["model_id"]: r["forecast_json"]
            for r in ledger.last_ok_forecasts("metaculus:123")
        }
        assert last == {"model-a": "0.6", "model-b": "0.5"}
        final_report = ledger.get_submissions(outcome.run_id)[1]["report"]
        assert "## Disagreement\n- stale_or_missing_information" in final_report
        assert "Second look A." in final_report and "MARKER-A1" not in final_report
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("label", "fixed_sentence"),
    [
        (
            "stale_or_missing_information",
            "The earlier research may have missed recent or relevant developments; "
            "weigh the newly gathered evidence carefully.",
        ),
        (
            "misread_resolution_criteria",
            "The resolution criteria or fine print may have been misread; read them "
            "literally, edge cases included, before forecasting.",
        ),
    ],
)
@pytest.mark.parametrize("luna_failure", ["error", "garbage"])
async def test_a_failed_query_writer_reruns_with_the_fixed_diagnosis_and_no_queries(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    label: str,
    fixed_sentence: str,
    luna_failure: str,
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, label)

    async def garbage() -> LLMResult:
        return await _ok("I cannot help with that.")

    async def down() -> LLMResult:
        raise LLMError("luna is down")

    llm = _two_model_llm(referee=down if luna_failure == "error" else garbage)
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert _posted_binary(metaculus) == pytest.approx(
            [FIRST_AVERAGE, SECOND_AVERAGE]
        )
        assert _referee_row(ledger, outcome.run_id) == (label, 1, "rerun_ok")
        assert len(_comment_texts(metaculus)) == 1
        assert "extra_queries" not in _iw_research_bodies(httpx_mock)[1]
        for _, prompt in llm.prompts[2:]:
            assert f"Likely cause: {fixed_sentence}" in prompt
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "label", ["different_base_rate", "genuine_uncertainty", "other"]
)
async def test_a_class_that_does_not_trigger_posts_the_first_average_as_the_final(
    tmp_path: Path, httpx_mock: HTTPXMock, label: str
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock)
    _mock_jev(httpx_mock, label)
    llm = _two_model_llm(referee=_luna("unused", ["unused"]))
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert _posted_binary(metaculus) == pytest.approx(
            [FIRST_AVERAGE, FIRST_AVERAGE]
        )
        assert _submission_rows(ledger, outcome.run_id) == [
            ("provisional", 0, pytest.approx(FIRST_AVERAGE)),
            ("final", 1, pytest.approx(FIRST_AVERAGE)),
        ]
        assert _referee_row(ledger, outcome.run_id) == (label, 0, "not_triggered")
        assert len(_comment_texts(metaculus)) == 1
        assert len(_iw_research_bodies(httpx_mock)) == 1
        assert llm.referee_prompts == []
        assert len(llm.prompts) == 2
        final_report = ledger.get_submissions(outcome.run_id)[1]["report"]
        assert f"## Disagreement\n- {label}" in final_report
    finally:
        ledger.close()


async def test_jev_failure_leaves_the_first_average_and_posts_the_final_at_once(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock)
    httpx_mock.add_response(
        url=JEV_URL, status_code=500, headers={"retry-after": "0"}, is_reusable=True
    )
    llm = _two_model_llm(referee=_luna("unused", ["unused"]))
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert len(httpx_mock.get_requests(url=JEV_URL)) == 2
        assert _posted_binary(metaculus) == pytest.approx(
            [FIRST_AVERAGE, FIRST_AVERAGE]
        )
        assert _referee_row(ledger, outcome.run_id) == (None, 0, "jev_failed")
        assert len(_comment_texts(metaculus)) == 1
        assert llm.referee_prompts == [] and len(llm.prompts) == 2
        final_report = ledger.get_submissions(outcome.run_id)[1]["report"]
        assert "## Disagreement" not in final_report
    finally:
        ledger.close()


@pytest.mark.parametrize("status", [422, 503], ids=["iw-rejects", "iw-down"])
async def test_an_iw_failure_leaves_the_provisional_standing_with_its_comment_at_hard(
    tmp_path: Path, httpx_mock: HTTPXMock, status: int
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, (status, {"detail": "no"}))
    if status == 503:
        httpx_mock.add_response(
            url=GOOGLE_REQUEST, text=GOOGLE_FIXTURE.read_text(encoding="utf-8")
        )
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(referee=_luna("The vote date moved.", ["vote date postponed"]))
    metaculus = FakeMetaculus()
    comment_times: list[datetime] = []
    post_comment = metaculus.post_comment

    async def timed_post_comment(question: object, text: str) -> None:
        comment_times.append(clock.now)
        await post_comment(question, text)

    metaculus.post_comment = timed_post_comment  # type: ignore[method-assign]
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "provisional"
        assert _posted_binary(metaculus) == pytest.approx([FIRST_AVERAGE])
        assert _submission_rows(ledger, outcome.run_id) == [
            ("provisional", 1, pytest.approx(FIRST_AVERAGE))
        ]
        (comment,) = _comment_texts(metaculus)
        assert comment.startswith("betomcat v1.0.0, provisional forecast")
        assert comment_times == [NOW + timedelta(seconds=35)]
        assert _referee_row(ledger, outcome.run_id) == (
            "stale_or_missing_information",
            1,
            "rerun_failed",
        )
        assert len(llm.prompts) == 2
        assert len(_iw_research_bodies(httpx_mock)) == 2
    finally:
        ledger.close()


async def test_a_missing_reforecast_leaves_the_first_average_standing(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, "misread_resolution_criteria")
    llm = _two_model_llm(
        second_b=LLMError("model-b is down"),
        referee=_luna("Clause two was misread.", ["resolution source"]),
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "provisional"
        # One re-forecast is never mixed with one first-round forecast.
        assert _posted_binary(metaculus) == pytest.approx([FIRST_AVERAGE])
        assert len(_comment_texts(metaculus)) == 1
        assert _referee_row(ledger, outcome.run_id) == (
            "misread_resolution_criteria",
            1,
            "rerun_failed",
        )
        rows = ledger.get_model_forecasts(outcome.run_id)
        assert sorted((r["model_id"], r["attempt"], r["status"]) for r in rows) == [
            ("model-a", -2, "discarded"),
            ("model-a", 1, "ok"),
            ("model-b", -2, "failed"),
            ("model-b", 1, "ok"),
        ]
        # Weight scoring reads the last `ok` row: still the submitted first round.
        last = {
            r["model_id"]: r["forecast_json"]
            for r in ledger.last_ok_forecasts("metaculus:123")
        }
        assert last == {"model-a": "0.65", "model-b": "0.4"}
    finally:
        ledger.close()


@pytest.mark.parametrize("soft_seconds", [10, 5], ids=["soft-first", "soft-is-hard"])
async def test_soft_or_hard_cutting_the_rerun_short_keeps_the_provisional(
    tmp_path: Path, httpx_mock: HTTPXMock, soft_seconds: int
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(
        second_a=HANG,
        second_b=HANG,
        referee=_luna("The vote date moved.", ["vote date postponed"]),
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(
            tmp_path,
            llm,
            clock,
            metaculus,
            ledger,
            soft_threshold=timedelta(seconds=soft_seconds),
        )

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "provisional"
        assert clock.now >= NOW + timedelta(seconds=35)
        assert _posted_binary(metaculus) == pytest.approx([FIRST_AVERAGE])
        assert _submission_rows(ledger, outcome.run_id) == [
            ("provisional", 1, pytest.approx(FIRST_AVERAGE))
        ]
        (comment,) = _comment_texts(metaculus)
        assert comment.startswith("betomcat v1.0.0, provisional forecast")
        assert _referee_row(ledger, outcome.run_id) == (
            "stale_or_missing_information",
            1,
            "abandoned",
        )
        assert len(llm.prompts) == 4
    finally:
        ledger.close()


async def test_a_rerun_still_running_at_soft_is_abandoned(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    reforecasts_ready = clock.release_at(NOW + timedelta(seconds=32))  # soft is +30
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(
        second_a=(reforecasts_ready, SECOND_A),
        second_b=(reforecasts_ready, SECOND_B),
        referee=_luna("The vote date moved.", ["vote date postponed"]),
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        # The re-forecasts would have landed before hard, but soft came first.
        assert outcome.status == "provisional"
        assert _posted_binary(metaculus) == pytest.approx([FIRST_AVERAGE])
        assert _submission_rows(ledger, outcome.run_id) == [
            ("provisional", 1, pytest.approx(FIRST_AVERAGE))
        ]
        assert len(_comment_texts(metaculus)) == 1
        assert _referee_row(ledger, outcome.run_id) == (
            "stale_or_missing_information",
            1,
            "abandoned",
        )
        assert len(llm.prompts) == 4
        assert [r["attempt"] for r in ledger.get_model_forecasts(outcome.run_id)] == [
            1,
            1,
        ]
    finally:
        ledger.close()


@pytest.mark.parametrize("jev_status", [200, 500], ids=["classified", "jev-down"])
async def test_a_divergence_found_after_soft_is_only_labelled(
    tmp_path: Path, httpx_mock: HTTPXMock, jev_status: int
) -> None:
    clock = FakeClock(NOW)
    release_b = clock.release_at(NOW + timedelta(seconds=31))
    _mock_iw(httpx_mock)
    if jev_status == 200:
        # A class that would trigger a re-run before soft: after soft it is a label.
        _mock_jev(httpx_mock, "stale_or_missing_information")
    else:
        httpx_mock.add_response(
            url=JEV_URL, status_code=500, headers={"retry-after": "0"}, is_reusable=True
        )

    async def script_b() -> LLMResult:
        await release_b.wait()
        return await _ok(FIRST_B)

    llm = ScriptedLLM(
        {"model-a": _replies(FIRST_A), "model-b": script_b},
        referee=_luna("unused", ["unused"]),
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        # model-a's lone provisional, then the final, which is the first average.
        assert _posted_binary(metaculus) == pytest.approx([0.65, FIRST_AVERAGE])
        assert [kind for kind, _, _ in _submission_rows(ledger, outcome.run_id)] == [
            "provisional",
            "final",
        ]
        assert len(_comment_texts(metaculus)) == 1
        assert len(_iw_research_bodies(httpx_mock)) == 1
        assert llm.referee_prompts == []
        final_report = ledger.get_submissions(outcome.run_id)[1]["report"]
        if jev_status == 200:
            assert _referee_row(ledger, outcome.run_id) == (
                "stale_or_missing_information",
                0,
                "late",
            )
            assert "## Disagreement\n- stale_or_missing_information" in final_report
        else:
            assert _referee_row(ledger, outcome.run_id) == (None, 0, "jev_failed")
            assert "## Disagreement" not in final_report
    finally:
        ledger.close()


@dataclass
class SecondCallGuard:
    """Lets everything through at the draw; on the referee's check, drops a model.

    `listed` says whether the dropped model also appears in `excluded_ids` (the
    context-fit filter drops models without listing them), `error` raises instead.
    """

    dropped_id: str = "model-b"
    listed: bool = True
    error: Exception | None = None
    prompt_chars: list[int] = field(default_factory=list)

    async def restrict(
        self, models: list[ModelSpec], prompt_chars: int, ledger: Ledger, now: object
    ) -> PacingResult:
        self.prompt_chars.append(prompt_chars)
        if len(self.prompt_chars) == 1:
            return PacingResult(models, 1.0, 8.0, [])
        if self.error is not None:
            raise self.error
        return PacingResult(
            [m for m in models if m.id != self.dropped_id],
            1.5,
            8.0,
            [self.dropped_id] if self.listed else [],
        )


@pytest.mark.parametrize("listed", [True, False], ids=["excluded", "silently-dropped"])
async def test_a_budget_excluded_model_skips_the_rerun(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
    listed: bool,
) -> None:
    caplog.set_level(logging.INFO)
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(referee=_luna("unused", ["unused"]))
    metaculus = FakeMetaculus()
    guard = SecondCallGuard(listed=listed)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger, budget=guard)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert "referee: re-run skipped (budget)" in caplog.messages
        # Asked before Luna or IW spend anything, with the first prompt's length.
        assert guard.prompt_chars == [len(llm.prompts[0][1])] * 2
        assert llm.referee_prompts == []
        assert len(llm.prompts) == 2
        assert len(_iw_research_bodies(httpx_mock)) == 1
        assert _posted_binary(metaculus) == pytest.approx(
            [FIRST_AVERAGE, FIRST_AVERAGE]
        )
        assert len(_comment_texts(metaculus)) == 1
        assert _referee_row(ledger, outcome.run_id) == (
            "stale_or_missing_information",
            0,
            "budget",
        )
        final_report = ledger.get_submissions(outcome.run_id)[1]["report"]
        assert "## Disagreement\n- stale_or_missing_information" in final_report
    finally:
        ledger.close()


async def test_an_unexpected_referee_error_falls_back_to_the_first_average(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(referee=_luna("unused", ["unused"]))
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(
            tmp_path,
            llm,
            clock,
            metaculus,
            ledger,
            budget=SecondCallGuard(error=RuntimeError("guard blew up")),
        )

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert _posted_binary(metaculus) == pytest.approx(
            [FIRST_AVERAGE, FIRST_AVERAGE]
        )
        assert len(_comment_texts(metaculus)) == 1
        assert _referee_row(ledger, outcome.run_id) == (
            "stale_or_missing_information",
            0,
            "error",
        )
    finally:
        ledger.close()


async def test_a_rerun_over_the_asknews_budget_asks_iw_for_free_providers_only(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(
        referee=_luna("The vote date moved.", ["vote date postponed", "second query"])
    )
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        # The first research (2 calls) fits 3; the second (2 + 2 queries) does not.
        deps = _referee_deps(
            tmp_path, llm, clock, metaculus, ledger, asknews_monthly_calls=3
        )

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        first_body, second_body = _iw_research_bodies(httpx_mock)
        assert "providers" not in first_body
        assert second_body["providers"] == list(FREE_NEWS_PROVIDERS)
        assert second_body["extra_queries"] == ["vote date postponed", "second query"]
        assert _asknews_rows(tmp_path) == [(outcome.run_id, 2)]
    finally:
        ledger.close()


async def test_forecasts_that_agree_never_reach_the_referee(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock)
    llm = _two_model_llm(first_b="Summary: close.\nProbability: 60%")
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert _posted_binary(metaculus) == pytest.approx([0.6375])
        assert _submission_rows(ledger, outcome.run_id) == [
            ("final", 1, pytest.approx(0.6375))
        ]
        assert httpx_mock.get_requests(url=JEV_URL) == []
        assert ledger.get_referee(outcome.run_id or 0) is None
    finally:
        ledger.close()


async def test_without_a_jev_client_a_divergence_is_posted_as_before(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock)
    llm = _two_model_llm()
    metaculus = FakeMetaculus()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, metaculus, ledger, jev=None)

        outcome = await _run_diverging(deps, clock)

        assert outcome.status == "submitted"
        assert _posted_binary(metaculus) == pytest.approx([FIRST_AVERAGE])
        assert _submission_rows(ledger, outcome.run_id) == [
            ("final", 1, pytest.approx(FIRST_AVERAGE))
        ]
        assert len(_comment_texts(metaculus)) == 1
        assert ledger.get_referee(outcome.run_id or 0) is None
    finally:
        ledger.close()


async def test_referee_logs_carry_class_names_and_ids_only(
    tmp_path: Path, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(NOW)
    _mock_iw(httpx_mock, SECOND_RESEARCH)
    _mock_jev(httpx_mock, "stale_or_missing_information")
    llm = _two_model_llm(
        referee=_luna("The vote date moved.", ["vote date postponed", "second query"])
    )
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        deps = _referee_deps(tmp_path, llm, clock, FakeMetaculus(), ledger)

        await _run_diverging(deps, clock)

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "referee: metaculus:123 class=stale_or_missing_information" in logged
        assert "referee: metaculus:123 re-run with 2 extra queries" in logged
        for secret in (
            *NO_LEAK,
            "Second look",
            "vote date",
            "second query",
            "The vote date moved",
            "0.5875",
            "0.575",
            "0.65",
            "58.75",
            "57.5",
            "jev_test_key",
        ):
            assert secret not in logged
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((0.64, 0.50), False),
        ((0.66, 0.50), True),
        ((0.50, 0.66), True),
    ],
)
def test_binary_divergence_bar_is_more_than_fifteen_points(
    values: tuple[float, float], expected: bool
) -> None:
    results = {
        mid: _model_result(mid, value) for mid, value in zip("ab", values, strict=True)
    }

    assert _diverged("binary", results, ["a", "b"]) is expected


def test_divergence_needs_exactly_two_models() -> None:
    results = {"a": _model_result("a", 0.9)}

    assert _diverged("binary", results, ["a"]) is False


def test_multiple_choice_divergence_bar_is_total_variation_above_a_fifth() -> None:
    near = {"x": _model_result("x", {"A": 0.5, "B": 0.5})}
    far = {"y": _model_result("y", {"A": 0.69, "B": 0.31})}
    farther = {"z": _model_result("z", {"A": 0.71, "B": 0.29})}

    assert _diverged("multiple_choice", {**near, **far}, ["x", "y"]) is False
    assert _diverged("multiple_choice", {**near, **farther}, ["x", "z"]) is True


def test_numeric_divergence_bar_is_a_quarter_of_the_range_between_medians() -> None:
    def cdf(median_index: int) -> list[float]:
        return [0.0 if i < median_index else 1.0 for i in range(101)]

    near = {"x": _model_result("x", cdf(40)), "y": _model_result("y", cdf(65))}
    far = {"x": _model_result("x", cdf(40)), "y": _model_result("y", cdf(66))}

    assert _diverged("numeric", near, ["x", "y"]) is False
    assert _diverged("numeric", far, ["x", "y"]) is True


def _model_result(model_id: str, value: object) -> ModelResult:
    return ModelResult(
        model_id=model_id,
        rationale="",
        cost_usd=0.0,
        tokens_in=0,
        tokens_out=0,
        value=value,  # type: ignore[arg-type]
        summary="",
    )
