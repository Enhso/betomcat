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

import orjson
import pytest
import yaml
from forecasting_tools.data_models.questions import BinaryQuestion

from betomcat.budget import PacingResult
from betomcat.ledger import Ledger
from betomcat.llm import LLMError, LLMResult
from betomcat.pipeline import PipelineDeps, run_pipeline
from betomcat.pool import DrawResult, ModelSpec, write_weights
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
