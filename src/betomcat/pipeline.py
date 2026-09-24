"""Per-question orchestrator: the spec s6 deadline ladder end to end.

`close = question.close_time`; `soft = close - soft_threshold`;
`hard = close - hard_threshold`. Steps: classify family -> research (wait
for it, bounded only by hard) -> draw (recorded in the ledger before any
model is called) -> call every drawn model concurrently, with a bounded
retry ladder before soft and one duplicate attempt per still-missing model
at soft. Submission policy (BUILD_LOG decision 4): the first model result
is submitted immediately as `provisional`; the full drawn set, once
complete, is submitted as `final`; nothing new is submitted once hard
arrives -- whatever was last submitted stands. If nothing was ever
submitted, the question is recorded `missed`: never a default number.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from forecasting_tools.data_models.questions import (
    BinaryQuestion,
    ConditionalQuestion,
    DateQuestion,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
)

from betomcat import reconcile
from betomcat.budget import BudgetGuard
from betomcat.comment import (
    CommentState,
    ModelForecastInfo,
    render_comment,
    render_report,
)
from betomcat.forecast import (
    ForecastParseError,
    ModelResult,
    PromptFields,
    QuestionKind,
    forecast_binary,
    forecast_multiple_choice,
    forecast_numeric,
    render_prompt,
)
from betomcat.ledger import Ledger
from betomcat.llm import LLMError, OpenRouterClient
from betomcat.metaculus import MetaculusWrapper
from betomcat.pool import (
    DrawResult,
    ModelSpec,
    PoolConfig,
    draw_replacement,
    effective_weight,
    load_pool,
    load_weights,
)
from betomcat.pool import draw as draw_pool
from betomcat.research import (
    FamilyClassification,
    IWClient,
    ResearchResult,
    render_claims_text,
    render_history_text,
)

logger = logging.getLogger(__name__)

RunStatus = Literal["submitted", "provisional", "missed", "failed", "skipped"]

# A drawn model that gives up (exhausts its attempt loop with no result)
# before the soft deadline gets substituted by a spare, up to this many
# substitutions per run -- caps the worst case (an unlucky run of dead
# models) rather than let replacement chase spares indefinitely.
MAX_REPLACEMENTS_PER_RUN = 4


async def default_referee(**kwargs: object) -> str | None:
    """No-op referee stub (this chunk). Never changes the reconciled number."""
    return None


@dataclass
class PipelineDeps:
    """Everything `run_pipeline` needs, injected for testability."""

    iw: IWClient
    llm: OpenRouterClient
    metaculus: MetaculusWrapper
    ledger: Ledger
    pool_path: Path
    data_dir: Path
    binary_clamp: tuple[float, float] = (0.01, 0.99)
    bot_version: str = "1.0.0"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rng: random.Random = field(default_factory=random.Random)
    referee: Callable[..., Awaitable[str | None]] = default_referee
    soft_threshold: timedelta = timedelta(minutes=30)
    hard_threshold: timedelta = timedelta(minutes=5)
    max_retries: int = 2
    retry_backoff: float = 2.0
    per_call_timeout: float = 180.0
    poll_interval: float = 1.0
    budget: BudgetGuard = field(default_factory=BudgetGuard)


@dataclass(frozen=True)
class PipelineOutcome:
    status: RunStatus
    run_id: int | None
    detail: str = ""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _question_key(question: MetaculusQuestion) -> str:
    return f"metaculus:{question.id_of_question}"


def _question_kind(
    question: MetaculusQuestion,
) -> tuple[QuestionKind, list[str] | None]:
    if isinstance(question, BinaryQuestion):
        return "binary", None
    if isinstance(question, MultipleChoiceQuestion):
        return "multiple_choice", question.options
    if isinstance(question, NumericQuestion | DateQuestion):
        return "numeric", None
    raise ValueError(f"unsupported question type: {type(question).__name__}")


def _build_prompt_fields(
    question: MetaculusQuestion,
    research: ResearchResult,
    kind: QuestionKind,
    options: list[str] | None,
    now: datetime,
) -> PromptFields:
    units = ""
    bounds_text = ""
    if kind == "numeric":
        units = question.unit_of_measure or ""
        lower = "open" if question.open_lower_bound else "closed"
        upper = "open" if question.open_upper_bound else "closed"
        bounds_text = (
            f"[{question.lower_bound}, {question.upper_bound}] "
            f"({lower} lower bound, {upper} upper bound)"
        )
    return PromptFields(
        title=question.question_text,
        background=question.background_info or "",
        resolution_criteria=question.resolution_criteria or "",
        fine_print=question.fine_print or "",
        today=now.strftime("%Y-%m-%d"),
        close_time=question.close_time.isoformat()
        if question.close_time
        else "unknown",
        resolve_time=(
            question.scheduled_resolution_time.isoformat()
            if question.scheduled_resolution_time
            else "unknown"
        ),
        briefing=research.briefing_text,
        claims=render_claims_text(research.claims),
        family_claims=render_claims_text(research.family_claims),
        history=render_history_text(research.history),
        units=units,
        bounds_text=bounds_text,
        options_list=", ".join(options) if options else "",
    )


async def _call_model(
    model: ModelSpec,
    kind: QuestionKind,
    options: list[str] | None,
    prompt: str,
    question: MetaculusQuestion,
    deps: PipelineDeps,
    timeout: float,
) -> ModelResult:
    if kind == "binary":
        return await forecast_binary(model, prompt, deps.llm, timeout)
    if kind == "multiple_choice":
        assert options is not None
        return await forecast_multiple_choice(model, prompt, deps.llm, timeout, options)
    assert isinstance(question, NumericQuestion | DateQuestion)
    return await forecast_numeric(model, prompt, deps.llm, timeout, question)


async def _model_attempt_loop(
    model: ModelSpec,
    kind: QuestionKind,
    options: list[str] | None,
    prompt: str,
    question: MetaculusQuestion,
    run_id: int,
    soft_deadline: datetime,
    hard_deadline: datetime,
    deps: PipelineDeps,
) -> ModelResult | None:
    """The initial call plus up to `max_retries` retries, retries gated by soft."""
    attempt = 0
    while True:
        now = deps.clock()
        if now >= hard_deadline:
            return None
        attempt += 1
        started_at = _iso(now)
        remaining = min((hard_deadline - now).total_seconds(), deps.per_call_timeout)
        try:
            result = await _call_model(
                model, kind, options, prompt, question, deps, remaining
            )
        except (LLMError, ForecastParseError) as exc:
            deps.ledger.record_model_forecast(
                run_id,
                model.id,
                attempt,
                started_at,
                _iso(deps.clock()),
                status="failed",
                forecast=None,
                rationale=None,
                cost_usd=None,
                tokens_in=None,
                tokens_out=None,
                error=str(exc),
            )
            if attempt > deps.max_retries or deps.clock() >= soft_deadline:
                return None
            await deps.sleep(deps.retry_backoff)
            continue
        deps.ledger.record_model_forecast(
            run_id,
            model.id,
            attempt,
            started_at,
            _iso(deps.clock()),
            status="ok",
            forecast=result.value,
            rationale=result.rationale,
            cost_usd=result.cost_usd,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            error=None,
        )
        return result


async def _single_attempt(
    model: ModelSpec,
    kind: QuestionKind,
    options: list[str] | None,
    prompt: str,
    question: MetaculusQuestion,
    run_id: int,
    hard_deadline: datetime,
    deps: PipelineDeps,
) -> ModelResult | None:
    """One lone-shot call (the soft-threshold duplicate attempt). No retries."""
    now = deps.clock()
    if now >= hard_deadline:
        return None
    started_at = _iso(now)
    remaining = min((hard_deadline - now).total_seconds(), deps.per_call_timeout)
    try:
        result = await _call_model(
            model, kind, options, prompt, question, deps, remaining
        )
    except (LLMError, ForecastParseError) as exc:
        deps.ledger.record_model_forecast(
            run_id,
            model.id,
            -1,
            started_at,
            _iso(deps.clock()),
            status="failed",
            forecast=None,
            rationale=None,
            cost_usd=None,
            tokens_in=None,
            tokens_out=None,
            error=str(exc),
        )
        return None
    deps.ledger.record_model_forecast(
        run_id,
        model.id,
        -1,
        started_at,
        _iso(deps.clock()),
        status="ok",
        forecast=result.value,
        rationale=result.rationale,
        cost_usd=result.cost_usd,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        error=None,
    )
    return result


def _median_index_fraction(cdf: list[float]) -> float:
    """Fraction of the way along `cdf` where it first crosses 0.5.

    Used as a cheap proxy for "median as a fraction of the question's
    range" without needing each model's nominal x-axis, since the CDF's
    evaluation points are evenly spaced across the range for every model on
    the same question.
    """
    n = len(cdf)
    for i, p in enumerate(cdf):
        if p >= 0.5:
            return i / max(n - 1, 1)
    return 1.0


async def _maybe_referee(
    kind: QuestionKind,
    results: dict[str, ModelResult],
    ordered_ids: list[str],
    deps: PipelineDeps,
) -> str | None:
    """Call the referee hook iff the two forecasts diverge past spec s5's bars."""
    if len(ordered_ids) != 2:
        return None
    a, b = results[ordered_ids[0]], results[ordered_ids[1]]
    if kind == "binary":
        prob_a, prob_b = cast(float, a.value), cast(float, b.value)
        diverged = abs(prob_a - prob_b) > 0.15
    elif kind == "multiple_choice":
        va = cast(dict[str, float], a.value)
        vb = cast(dict[str, float], b.value)
        keys = set(va) | set(vb)
        total_variation = 0.5 * sum(abs(va.get(k, 0.0) - vb.get(k, 0.0)) for k in keys)
        diverged = total_variation > 0.2
    else:
        cdf_a = cast(list[float], a.value)
        cdf_b = cast(list[float], b.value)
        diverged = (
            abs(_median_index_fraction(cdf_a) - _median_index_fraction(cdf_b)) > 0.25
        )
    if not diverged:
        return None
    return await deps.referee(kind=kind, model_a=a, model_b=b)


async def _submit(
    *,
    question: MetaculusQuestion,
    kind: QuestionKind,
    results: dict[str, ModelResult],
    ordered_ids: list[str],
    weights: dict[str, float],
    draw_result: DrawResult,
    family: FamilyClassification,
    research: ResearchResult,
    run_id: int,
    submission_kind: Literal["provisional", "final"],
    pacing_note: str | None,
    deps: PipelineDeps,
) -> None:
    final_value: object
    arithmetic: str
    if kind == "binary":
        binary_triples = [
            (mid, cast(float, results[mid].value), weights[mid]) for mid in ordered_ids
        ]
        binary_rec = reconcile.reconcile_binary(binary_triples, deps.binary_clamp)
        final_value = binary_rec.probability
        arithmetic = binary_rec.arithmetic
        await deps.metaculus.post_binary(question, binary_rec.probability)
    elif kind == "multiple_choice":
        mc_triples = [
            (mid, cast(dict[str, float], results[mid].value), weights[mid])
            for mid in ordered_ids
        ]
        mc_rec = reconcile.reconcile_multiple_choice(mc_triples)
        final_value = mc_rec.options
        arithmetic = mc_rec.arithmetic
        await deps.metaculus.post_multiple_choice(question, mc_rec.options)
    else:
        numeric_triples = [
            (mid, cast(list[float], results[mid].value), weights[mid])
            for mid in ordered_ids
        ]
        numeric_rec = reconcile.reconcile_numeric(numeric_triples)
        final_value = numeric_rec.cdf
        arithmetic = numeric_rec.arithmetic
        await deps.metaculus.post_numeric(question, numeric_rec.cdf)

    referee_type = None
    if submission_kind == "final":
        referee_type = await _maybe_referee(kind, results, ordered_ids, deps)

    comment_state = CommentState(
        bot_version=deps.bot_version,
        kind=submission_kind,
        timestamp=_iso(deps.clock()),
        degraded=research.degraded,
        family_id=family.family_id,
        family_label=family.label,
        family_decision=family.decision,
        family_probability=family.probability,
        draw=draw_result,
        model_forecasts=[
            ModelForecastInfo(
                mid, results[mid].value, results[mid].summary, results[mid].rationale
            )
            for mid in ordered_ids
        ],
        arithmetic=arithmetic,
        pacing_note=pacing_note,
        referee_type=referee_type,
        history=research.history,
        claims=research.claims,
    )
    comment_text = render_comment(comment_state)
    await deps.metaculus.post_comment(question, comment_text)
    deps.ledger.record_submission(
        run_id,
        submission_kind,
        final_value,
        comment_posted=True,
        report=render_report(comment_state),
    )


def _slot_draw_result(
    draw_result: DrawResult, slots: list[ModelSpec], weights: dict[str, float]
) -> DrawResult:
    """`draw_result`, refreshed to reflect the current (possibly replaced) slots."""
    return replace(
        draw_result,
        models=[m.id for m in slots],
        weights={m.id: weights[m.id] for m in slots},
    )


async def _run_model_ladder(
    *,
    question: MetaculusQuestion,
    kind: QuestionKind,
    options: list[str] | None,
    prompt: str,
    drawn: list[ModelSpec],
    weights: dict[str, float],
    draw_result: DrawResult,
    family: FamilyClassification,
    research: ResearchResult,
    run_id: int,
    soft_deadline: datetime,
    hard_deadline: datetime,
    pacing_note: str | None,
    spares: list[ModelSpec],
    draw_weights: dict[str, float],
    deps: PipelineDeps,
) -> RunStatus:
    slots: list[ModelSpec] = list(drawn)
    weights = dict(weights)
    untried_spares = list(spares)
    replacements_used = 0

    tasks: dict[str, asyncio.Task[ModelResult | None]] = {
        model.id: asyncio.create_task(
            _model_attempt_loop(
                model,
                kind,
                options,
                prompt,
                question,
                run_id,
                soft_deadline,
                hard_deadline,
                deps,
            )
        )
        for model in drawn
    }
    duplicate_tasks: dict[str, asyncio.Task[ModelResult | None]] = {}
    duplicated: set[str] = set()
    results: dict[str, ModelResult] = {}
    submitted: RunStatus | None = None

    def _collect(task_map: dict[str, asyncio.Task[ModelResult | None]]) -> None:
        for model_id, task in task_map.items():
            if task.done() and model_id not in results:
                try:
                    result = task.result()
                except (asyncio.CancelledError, Exception):
                    result = None
                if result is not None:
                    results[model_id] = result

    while True:
        _collect(tasks)
        _collect(duplicate_tasks)

        now = deps.clock()

        if now < soft_deadline:
            for i in range(len(slots)):
                if replacements_used >= MAX_REPLACEMENTS_PER_RUN or not untried_spares:
                    break
                slot = slots[i]
                if tasks[slot.id].done() and slot.id not in results:
                    replacement = draw_replacement(
                        deps.rng, untried_spares, draw_weights
                    )
                    untried_spares.remove(replacement)
                    weight = effective_weight(replacement.id, draw_weights)
                    weights[replacement.id] = weight
                    deps.ledger.record_draw(
                        run_id, {replacement.id: weight}, draw_result.pool_avg, False
                    )
                    logger.info(
                        "model %s gave up before soft deadline; replacing with %s",
                        slot.id,
                        replacement.id,
                    )
                    tasks[replacement.id] = asyncio.create_task(
                        _model_attempt_loop(
                            replacement,
                            kind,
                            options,
                            prompt,
                            question,
                            run_id,
                            soft_deadline,
                            hard_deadline,
                            deps,
                        )
                    )
                    slots[i] = replacement
                    replacements_used += 1

        if all(slot.id in results for slot in slots):
            ordered = [m.id for m in slots]
            await _submit(
                question=question,
                kind=kind,
                results=results,
                ordered_ids=ordered,
                weights=weights,
                draw_result=_slot_draw_result(draw_result, slots, weights),
                family=family,
                research=research,
                run_id=run_id,
                submission_kind="final",
                pacing_note=pacing_note,
                deps=deps,
            )
            submitted = "submitted"
            break

        if results and submitted is None:
            ordered = list(results.keys())
            await _submit(
                question=question,
                kind=kind,
                results=results,
                ordered_ids=ordered,
                weights=weights,
                draw_result=_slot_draw_result(draw_result, slots, weights),
                family=family,
                research=research,
                run_id=run_id,
                submission_kind="provisional",
                pacing_note=pacing_note,
                deps=deps,
            )
            submitted = "provisional"

        if now >= hard_deadline:
            for task in [*tasks.values(), *duplicate_tasks.values()]:
                if not task.done():
                    task.cancel()
            break

        if now >= soft_deadline:
            for slot in slots:
                if slot.id not in results and slot.id not in duplicated:
                    duplicated.add(slot.id)
                    duplicate_tasks[slot.id] = asyncio.create_task(
                        _single_attempt(
                            slot,
                            kind,
                            options,
                            prompt,
                            question,
                            run_id,
                            hard_deadline,
                            deps,
                        )
                    )

        await deps.sleep(deps.poll_interval)

    await asyncio.gather(
        *tasks.values(), *duplicate_tasks.values(), return_exceptions=True
    )
    return submitted or "missed"


async def run_pipeline(
    question: MetaculusQuestion, deps: PipelineDeps
) -> PipelineOutcome:
    """Run the full deadline ladder for one question, start to finish."""
    if isinstance(question, ConditionalQuestion):
        return PipelineOutcome("skipped", None, "conditional questions are unsupported")
    if question.already_forecasted:
        return PipelineOutcome("skipped", None, "already forecasted")
    if question.close_time is None:
        return PipelineOutcome("skipped", None, "question has no close_time")

    question_id = _question_key(question)
    close_time = question.close_time
    soft_deadline = close_time - deps.soft_threshold
    hard_deadline = close_time - deps.hard_threshold

    try:
        kind, _options = _question_kind(question)
    except ValueError as exc:
        return PipelineOutcome("skipped", None, str(exc))

    deps.ledger.upsert_question(
        question_id,
        question.question_text,
        kind,
        close_time.isoformat(),
        question.page_url,
    )
    run = deps.ledger.start_run(question_id)

    if deps.clock() >= hard_deadline:
        deps.ledger.finish_run(run.id, status="missed")
        return PipelineOutcome("missed", run.id, "already past hard deadline at start")

    try:
        return await _run_pipeline_inner(
            question, question_id, kind, run.id, soft_deadline, hard_deadline, deps
        )
    except Exception:
        logger.exception("pipeline failed for %s", question_id)
        deps.ledger.finish_run(run.id, status="failed")
        return PipelineOutcome("failed", run.id, "unhandled exception")


async def _run_pipeline_inner(
    question: MetaculusQuestion,
    question_id: str,
    kind: QuestionKind,
    run_id: int,
    soft_deadline: datetime,
    hard_deadline: datetime,
    deps: PipelineDeps,
) -> PipelineOutcome:
    _, options = _question_kind(question)
    context = {
        "resolution_criteria": question.resolution_criteria,
        "fine_print": question.fine_print,
        "background": question.background_info,
    }

    family = await deps.iw.classify_family(question.question_text, question_id, context)

    remaining = (hard_deadline - deps.clock()).total_seconds()
    if remaining <= 0:
        deps.ledger.finish_run(run_id, status="missed", family_id=family.family_id)
        return PipelineOutcome("missed", run_id, "hard deadline hit before research")

    research = await deps.iw.research(
        question=question.question_text,
        question_id=question_id,
        context=context,
        family_id=family.family_id,
        timeout=remaining,
    )

    if deps.clock() >= hard_deadline:
        deps.ledger.finish_run(
            run_id,
            status="missed",
            family_id=family.family_id,
            degraded=research.degraded,
            dossier_id=research.dossier_id,
        )
        return PipelineOutcome("missed", run_id, "hard deadline hit during research")

    prompt_fields = _build_prompt_fields(
        question, research, kind, options, deps.clock()
    )
    prompt = render_prompt(kind, prompt_fields)

    pool = load_pool(deps.pool_path)
    weights = load_weights(deps.data_dir)

    # Budget pacing (spec s5's draw runs unchanged; only the pool it sees is
    # narrowed here, and pool_avg is computed over this eligible set).
    pacing = await deps.budget.restrict(
        pool.enabled_models, len(prompt), deps.ledger, deps.clock()
    )
    deps.ledger.record_pacing_decision(
        run_id, pacing.pace, pacing.daily_budget, pacing.excluded_ids
    )
    pacing_note = (
        f"Pool restricted by budget pacing: excluded {', '.join(pacing.excluded_ids)}"
        if pacing.excluded_ids
        else None
    )
    restricted_pool = PoolConfig(
        ensemble_width=pool.ensemble_width, models=pacing.eligible
    )

    draw_result = draw_pool(restricted_pool, weights, deps.rng)
    deps.ledger.record_draw(
        run_id, draw_result.weights, draw_result.pool_avg, draw_result.fallback_fired
    )
    if draw_result.fallback_fired:
        deps.ledger.record_fallback(run_id, draw_result.models[0])

    model_specs = {m.id: m for m in pacing.eligible}
    drawn_specs = [model_specs[mid] for mid in draw_result.models]
    drawn_ids = set(draw_result.models)
    spares = [m for m in pacing.eligible if m.id not in drawn_ids]

    status = await _run_model_ladder(
        question=question,
        kind=kind,
        options=options,
        prompt=prompt,
        drawn=drawn_specs,
        weights=draw_result.weights,
        draw_result=draw_result,
        family=family,
        research=research,
        run_id=run_id,
        soft_deadline=soft_deadline,
        hard_deadline=hard_deadline,
        pacing_note=pacing_note,
        spares=spares,
        draw_weights=weights,
        deps=deps,
    )

    deps.ledger.finish_run(
        run_id,
        status=status,
        family_id=family.family_id,
        degraded=research.degraded,
        dossier_id=research.dossier_id,
    )
    detail = (
        ""
        if status != "missed"
        else "no model produced a forecast before hard deadline"
    )
    return PipelineOutcome(status, run_id, detail)
