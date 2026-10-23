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
Each question gets exactly one posted comment, coherent with whichever
forecast stands at close (Hatim, 2026-09-25): a `provisional` submission
posts its forecast but withholds its comment; the comment posts only
with the `final` submission, or, if hard arrives with the provisional
still standing and no final ever came, the comment is composed and posted
right then. Composing means one cheap synthesis call for a single rationale
paragraph (`rationale.py`); if it fails or there is no time for it, the
per-model summary comment is posted instead, so a comment is never missing.

Referee gate (HANDOFF s5.1; only when a Jev client is configured). When both
drawn models are in and diverge before soft, their weighted average is submitted
as `provisional` and a referee task classifies the disagreement. A fixable class
(stale information, misread criteria) triggers one re-run: targeted research,
then both models forecast again, and the final is the weighted average of the
two re-forecasts. Any other outcome without a re-run submits the first average
as the final right away. A re-run that fails, or is cut off by soft or hard,
leaves the provisional standing and its comment posts at hard. After soft a
divergence is only labelled. The referee never changes the arithmetic, only
whether a second round happens, and any failure of it falls back to the first
average.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
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
    render_synthesized_comment,
)
from betomcat.config import ASKNEWS_CYCLE_DAY, ASKNEWS_MONTHLY_CALLS
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
from betomcat.rationale import ForecastValue, synthesize_rationale
from betomcat.referee import (
    QUERY_WRITER_TIMEOUT,
    RERUN_CLASSES,
    DisagreementClass,
    JevClient,
    classify_disagreement,
    render_rerun_note,
    write_targeted_queries,
)
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

# IW's default research makes two AskNews latest-news calls (title query and
# keyword query, one call each); this is what one research is planned to spend.
ASKNEWS_CALLS_PER_RESEARCH = 2

# Asked of IW instead of its defaults once the AskNews cycle budget is spent.
FREE_NEWS_PROVIDERS = ("google_news", "bing_news", "wikipedia")

# `model_forecasts.attempt` of a referee re-forecast (-1 is the soft duplicate).
RERUN_ATTEMPT = -2


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
    jev: JevClient | None = None
    soft_threshold: timedelta = timedelta(minutes=30)
    hard_threshold: timedelta = timedelta(minutes=5)
    max_retries: int = 2
    retry_backoff: float = 2.0
    per_call_timeout: float = 180.0
    poll_interval: float = 1.0
    budget: BudgetGuard = field(default_factory=BudgetGuard)
    rationale_timeout_final: float = 90.0
    rationale_timeout_provisional: float = 45.0
    rationale_close_margin: timedelta = timedelta(seconds=60)
    asknews_monthly_calls: int = ASKNEWS_MONTHLY_CALLS


@dataclass(frozen=True)
class PipelineOutcome:
    status: RunStatus
    run_id: int | None
    detail: str = ""


@dataclass(frozen=True)
class _SubmittedForecast:
    """What `_submit` posted, kept so its comment can be composed later."""

    state: CommentState
    value: ForecastValue


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def asknews_cycle_start(now: datetime) -> datetime:
    """Start of the AskNews cycle containing `now`: 00:00 UTC on the cycle day.

    A cycle runs from `ASKNEWS_CYCLE_DAY` of one month to the day before it in
    the next, e.g. 22 Dec-21 Jan (the year rolls over with the month).
    """
    now = now.astimezone(UTC)
    year, month = now.year, now.month
    if now.day < ASKNEWS_CYCLE_DAY:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return datetime(year, month, ASKNEWS_CYCLE_DAY, tzinfo=UTC)


def _asknews_allowed(
    deps: PipelineDeps, planned_calls: int = ASKNEWS_CALLS_PER_RESEARCH
) -> bool:
    """Whether a research making `planned_calls` AskNews calls fits this cycle."""
    spent = deps.ledger.asknews_calls_since(_iso(asknews_cycle_start(deps.clock())))
    allowed = spent + planned_calls <= deps.asknews_monthly_calls
    logger.info(
        "asknews budget: %d/%d used this cycle; asknews %s",
        spent,
        deps.asknews_monthly_calls,
        "included" if allowed else "excluded",
    )
    return allowed


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


def _question_context(question: MetaculusQuestion) -> dict[str, str | None]:
    return {
        "resolution_criteria": question.resolution_criteria,
        "fine_print": question.fine_print,
        "background": question.background_info,
    }


async def _research(
    question: MetaculusQuestion,
    question_id: str,
    family_id: str | None,
    timeout: float,
    run_id: int,
    deps: PipelineDeps,
    extra_queries: list[str] | None = None,
) -> ResearchResult:
    """One IW research for `question`, its AskNews calls recorded against `run_id`.

    Each of `extra_queries` is one more AskNews call on IW's side, so it counts
    toward the cycle budget like the two default queries do.
    """
    planned_calls = ASKNEWS_CALLS_PER_RESEARCH + len(extra_queries or [])
    asknews_allowed = _asknews_allowed(deps, planned_calls)
    research = await deps.iw.research(
        question=question.question_text,
        question_id=question_id,
        context=_question_context(question),
        family_id=family_id,
        timeout=timeout,
        providers=None if asknews_allowed else list(FREE_NEWS_PROVIDERS),
        asknews_allowed=asknews_allowed,
        extra_queries=extra_queries or None,
    )
    # IW's planned calls count even if AskNews or IW failed (we cannot tell
    # from here, and over-counting is the safe side); the degraded fallback
    # adds the call it made itself, if any.
    asknews_calls = research.asknews_calls
    if asknews_allowed:
        asknews_calls += planned_calls
    if asknews_calls:
        deps.ledger.record_asknews_calls(run_id, asknews_calls)
    return research


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


def _diverged(
    kind: QuestionKind, results: dict[str, ModelResult], ordered_ids: list[str]
) -> bool:
    """Whether the two forecasts diverge past spec s5's bars (never for != 2)."""
    if len(ordered_ids) != 2:
        return False
    a, b = results[ordered_ids[0]], results[ordered_ids[1]]
    if kind == "binary":
        return abs(cast(float, a.value) - cast(float, b.value)) > 0.15
    if kind == "multiple_choice":
        va = cast(dict[str, float], a.value)
        vb = cast(dict[str, float], b.value)
        keys = set(va) | set(vb)
        return 0.5 * sum(abs(va.get(k, 0.0) - vb.get(k, 0.0)) for k in keys) > 0.2
    cdf_a = cast(list[float], a.value)
    cdf_b = cast(list[float], b.value)
    return abs(_median_index_fraction(cdf_a) - _median_index_fraction(cdf_b)) > 0.25


async def _post_comment(
    question: MetaculusQuestion,
    kind: QuestionKind,
    submitted: _SubmittedForecast,
    timeout_cap: float,
    not_after: datetime,
    deps: PipelineDeps,
) -> None:
    """Post the question's one comment: a synthesized rationale, else the fallback.

    The synthesis call gets `timeout_cap` seconds, shortened so it ends by
    `not_after`; with no time left it is skipped. Logs only the question id --
    the repo's Actions logs are public.
    """
    question_key = _question_key(question)
    timeout = min(timeout_cap, (not_after - deps.clock()).total_seconds())
    rationale = None
    if timeout > 0:
        rationale = await synthesize_rationale(
            question,
            kind,
            submitted.value,
            submitted.state.model_forecasts,
            deps.llm,
            timeout,
        )
    if rationale is None:
        logger.info("rationale synthesis failed, posting fallback for %s", question_key)
        text = render_comment(submitted.state)
    else:
        logger.info("rationale synthesis ok for %s", question_key)
        text = render_synthesized_comment(submitted.state, rationale)
    await deps.metaculus.post_comment(question, text)


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
    hard_deadline: datetime,
    deps: PipelineDeps,
    referee_type: DisagreementClass | None = None,
    label_disagreement: bool = False,
) -> _SubmittedForecast:
    """Post the forecast, record the submission, and return what was submitted.

    `referee_type` is the disagreement class already settled for this forecast,
    shown in its audit report. `label_disagreement` classifies the divergence
    right after the forecast is posted (the after-soft, label-only case).

    Each question gets exactly one posted comment (Hatim, 2026-09-25): a
    `final` submission posts its comment immediately (synthesis, if any, ends
    by `hard_deadline`); a `provisional` submission withholds it, recording
    `comment_posted=False` -- the caller composes and posts it from the
    returned `_SubmittedForecast` only if hard arrives with the provisional
    still standing.
    """
    final_value: ForecastValue
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

    if label_disagreement:
        referee_type = await _label_disagreement(
            question, [results[mid].rationale for mid in ordered_ids], run_id, deps
        )

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
    submitted = _SubmittedForecast(comment_state, final_value)
    comment_posted = submission_kind == "final"
    if comment_posted:
        await _post_comment(
            question,
            kind,
            submitted,
            deps.rationale_timeout_final,
            hard_deadline,
            deps,
        )
    deps.ledger.record_submission(
        run_id,
        submission_kind,
        final_value,
        comment_posted=comment_posted,
        report=render_report(comment_state),
    )
    return submitted


def _resolution_text(question: MetaculusQuestion) -> str:
    return "\n\n".join(
        text for text in (question.resolution_criteria, question.fine_print) if text
    )


async def _label_disagreement(
    question: MetaculusQuestion,
    rationales: Sequence[str],
    run_id: int,
    deps: PipelineDeps,
) -> DisagreementClass | None:
    """Label-only referee for a divergence found after soft: no re-run is possible.

    Fails open: any failure leaves the forecast unlabelled.
    """
    assert deps.jev is not None
    try:
        label = await classify_disagreement(
            deps.jev, question.question_text, _resolution_text(question), rationales
        )
        deps.ledger.record_referee(
            run_id, label, False, "late" if label is not None else "jev_failed"
        )
    except Exception as exc:
        logger.warning("referee: labelling failed (%s)", type(exc).__name__)
        return None
    logger.info(
        "referee: %s class=%s (label only, after soft)", _question_key(question), label
    )
    return label


@dataclass(frozen=True)
class _FirstRound:
    """The round that diverged, as the referee needs it."""

    question: MetaculusQuestion
    kind: QuestionKind
    options: list[str] | None
    prompt: str
    slots: list[ModelSpec]
    results: dict[str, ModelResult]
    family: FamilyClassification
    run_id: int
    hard_deadline: datetime


@dataclass
class _RefereeRun:
    """What the referee task settled, filled in as it goes.

    Lives outside the task so a cancel (soft or hard) keeps what was known:
    `outcome` stays `abandoned` unless the task reached an exit. `final_now` says
    nothing is pending, so the final can be submitted right away: from `results`
    (the re-forecasts, with their `research`) if set, else from the first round.
    """

    label: DisagreementClass | None = None
    rerun: bool = False
    outcome: str = "abandoned"
    final_now: bool = False
    results: dict[str, ModelResult] | None = None
    research: ResearchResult | None = None


async def _both_eligible(first: _FirstRound, deps: PipelineDeps) -> bool:
    """Whether the budget guard still lets both drawn models run (credit is tight).

    Asked before the re-run spends anything. A drawn model absent from the
    eligible set counts as excluded even when `excluded_ids` omits it (the
    context-fit filter drops models silently).
    """
    pool = load_pool(deps.pool_path)
    pacing = await deps.budget.restrict(
        pool.enabled_models, len(first.prompt), deps.ledger, deps.clock()
    )
    eligible = {model.id for model in pacing.eligible}
    return all(
        model.id in eligible and model.id not in pacing.excluded_ids
        for model in first.slots
    )


async def _reforecast(
    first: _FirstRound, prompt: str, deps: PipelineDeps
) -> dict[str, ModelResult] | None:
    """Both drawn models forecast `prompt` once, concurrently; all or nothing.

    The final needs both re-forecasts, so a missing one fails the re-run. Rows go
    to the ledger either way (cost is cost): `ok` when both came back, else the
    survivor is `discarded`, so weight scoring (which reads `ok` rows) never
    scores a forecast that was not submitted.
    """
    now = deps.clock()
    remaining = min((first.hard_deadline - now).total_seconds(), deps.per_call_timeout)
    if remaining <= 0:
        return None
    started_at = _iso(now)
    outcomes = await asyncio.gather(
        *(
            _call_model(
                model,
                first.kind,
                first.options,
                prompt,
                first.question,
                deps,
                remaining,
            )
            for model in first.slots
        ),
        return_exceptions=True,
    )
    finished_at = _iso(deps.clock())
    complete = all(isinstance(outcome, ModelResult) for outcome in outcomes)
    results: dict[str, ModelResult] = {}
    for model, outcome in zip(first.slots, outcomes, strict=True):
        if isinstance(outcome, ModelResult):
            results[model.id] = outcome
            deps.ledger.record_model_forecast(
                first.run_id,
                model.id,
                RERUN_ATTEMPT,
                started_at,
                finished_at,
                status="ok" if complete else "discarded",
                forecast=outcome.value,
                rationale=outcome.rationale,
                cost_usd=outcome.cost_usd,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                error=None,
            )
        else:
            deps.ledger.record_model_forecast(
                first.run_id,
                model.id,
                RERUN_ATTEMPT,
                started_at,
                finished_at,
                status="failed",
                forecast=None,
                rationale=None,
                cost_usd=None,
                tokens_in=None,
                tokens_out=None,
                error=str(outcome),
            )
    return results if complete else None


async def _rerun(
    first: _FirstRound,
    run: _RefereeRun,
    label: DisagreementClass,
    deps: PipelineDeps,
) -> bool:
    """The second round: targeted research, then both models forecast again.

    Each model gets the refreshed research and the one-line diagnosis, never the
    other model's rationale (that would herd them).

    Returns:
        True with `run.results` and `run.research` set when both re-forecasts are
        in, False when the round could not finish.
    """
    question_key = _question_key(first.question)
    rationales = [first.results[model.id].rationale for model in first.slots]
    remaining = (first.hard_deadline - deps.clock()).total_seconds()
    if remaining <= 0:
        return False
    plan = await write_targeted_queries(
        deps.llm,
        first.question.question_text,
        _resolution_text(first.question),
        label,
        rationales,
        min(QUERY_WRITER_TIMEOUT, remaining),
    )
    logger.info(
        "referee: %s re-run with %d extra queries", question_key, len(plan.queries)
    )
    remaining = (first.hard_deadline - deps.clock()).total_seconds()
    if remaining <= 0:
        return False
    research = await _research(
        first.question,
        question_key,
        first.family.family_id,
        remaining,
        first.run_id,
        deps,
        plan.queries,
    )
    if research.degraded:
        logger.warning("referee: %s re-run research degraded", question_key)
        return False
    fields = _build_prompt_fields(
        first.question, research, first.kind, first.options, deps.clock()
    )
    note = render_rerun_note(plan.diagnosis)
    prompt = render_prompt(
        first.kind, replace(fields, briefing=f"{fields.briefing}\n\n{note}")
    )
    results = await _reforecast(first, prompt, deps)
    if results is None:
        logger.warning("referee: %s re-forecast incomplete", question_key)
        return False
    run.results, run.research = results, research
    return True


async def _referee_steps(
    first: _FirstRound, run: _RefereeRun, deps: PipelineDeps
) -> None:
    assert deps.jev is not None
    question_key = _question_key(first.question)
    run.label = await classify_disagreement(
        deps.jev,
        first.question.question_text,
        _resolution_text(first.question),
        [first.results[model.id].rationale for model in first.slots],
    )
    if run.label is None:
        run.outcome, run.final_now = "jev_failed", True
        return
    logger.info("referee: %s class=%s", question_key, run.label)
    if run.label not in RERUN_CLASSES:
        run.outcome, run.final_now = "not_triggered", True
        return
    if not await _both_eligible(first, deps):
        logger.info("referee: re-run skipped (budget)")
        run.outcome, run.final_now = "budget", True
        return
    run.rerun = True
    reran = await _rerun(first, run, run.label, deps)
    run.outcome = "rerun_ok" if reran else "rerun_failed"
    run.final_now = reran


async def _run_referee(
    first: _FirstRound, run: _RefereeRun, deps: PipelineDeps
) -> None:
    """Classify the disagreement and, for a fixable class, run one second round.

    Never raises: a failure before the re-run falls back to submitting the first
    average now; a failure during it leaves the provisional standing.
    """
    try:
        await _referee_steps(first, run, deps)
    except Exception as exc:
        logger.warning(
            "referee: %s failed (%s)", _question_key(first.question), type(exc).__name__
        )
        run.results = run.research = None
        run.outcome = "rerun_failed" if run.rerun else "error"
        run.final_now = not run.rerun


def _record_referee(run_id: int, run: _RefereeRun, deps: PipelineDeps) -> None:
    deps.ledger.record_referee(run_id, run.label, run.rerun, run.outcome)


def _with_label(
    submitted: _SubmittedForecast, label: DisagreementClass | None
) -> _SubmittedForecast:
    return replace(submitted, state=replace(submitted.state, referee_type=label))


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
    provisional: _SubmittedForecast | None = None
    referee_task: asyncio.Task[None] | None = None
    referee_run = _RefereeRun()
    referee_settled = False

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
            if referee_task is None:
                diverged = deps.jev is not None and _diverged(kind, results, ordered)
                if diverged and now < soft_deadline:
                    provisional = await _submit(
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
                        hard_deadline=hard_deadline,
                        deps=deps,
                    )
                    submitted = "provisional"
                    first_round = _FirstRound(
                        question=question,
                        kind=kind,
                        options=options,
                        prompt=prompt,
                        slots=list(slots),
                        results={mid: results[mid] for mid in ordered},
                        family=family,
                        run_id=run_id,
                        hard_deadline=hard_deadline,
                    )
                    referee_task = asyncio.create_task(
                        _run_referee(first_round, referee_run, deps)
                    )
                else:
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
                        hard_deadline=hard_deadline,
                        deps=deps,
                        label_disagreement=diverged,
                    )
                    submitted = "submitted"
                    break
            elif referee_task.done() and not referee_settled:
                referee_settled = True
                _record_referee(run_id, referee_run, deps)
                if (
                    referee_run.final_now
                    and not referee_task.cancelled()
                    and now < hard_deadline
                ):
                    reforecasts = referee_run.results or results
                    await _submit(
                        question=question,
                        kind=kind,
                        results=reforecasts,
                        ordered_ids=ordered,
                        weights=weights,
                        draw_result=_slot_draw_result(draw_result, slots, weights),
                        family=family,
                        research=referee_run.research or research,
                        run_id=run_id,
                        submission_kind="final",
                        pacing_note=pacing_note,
                        hard_deadline=hard_deadline,
                        deps=deps,
                        referee_type=referee_run.label,
                    )
                    submitted = "submitted"
                    break
                assert provisional is not None
                provisional = _with_label(provisional, referee_run.label)

        if results and submitted is None:
            ordered = list(results.keys())
            provisional = await _submit(
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
                hard_deadline=hard_deadline,
                deps=deps,
            )
            submitted = "provisional"

        if now >= hard_deadline:
            for task in [*tasks.values(), *duplicate_tasks.values()]:
                if not task.done():
                    task.cancel()
            if referee_task is not None and not referee_settled:
                referee_task.cancel()
                await asyncio.gather(referee_task, return_exceptions=True)
                referee_settled = True
                _record_referee(run_id, referee_run, deps)
                assert provisional is not None
                provisional = _with_label(provisional, referee_run.label)
            if submitted == "provisional":
                assert provisional is not None
                assert question.close_time is not None
                await _post_comment(
                    question,
                    kind,
                    provisional,
                    deps.rationale_timeout_provisional,
                    question.close_time - deps.rationale_close_margin,
                    deps,
                )
                deps.ledger.mark_comment_posted(run_id, "provisional")
            break

        if now >= soft_deadline:
            if referee_task is not None and not referee_task.done():
                referee_task.cancel()
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

    family = await deps.iw.classify_family(
        question.question_text, question_id, _question_context(question)
    )

    remaining = (hard_deadline - deps.clock()).total_seconds()
    if remaining <= 0:
        deps.ledger.finish_run(run_id, status="missed", family_id=family.family_id)
        return PipelineOutcome("missed", run_id, "hard deadline hit before research")

    research = await _research(
        question, question_id, family.family_id, remaining, run_id, deps
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
