"""Prompt rendering and per-model forecast calls, for all three question types.

Prompts are tradecraft and live in `prompts/*.md`, not in code (BUILD_LOG
decision 8). This module fills the templates, calls the LLM, and parses the
response with `forecasting_tools`' `PredictionExtractor`. A parse failure
raises `ForecastParseError`, which the pipeline's retry ladder treats the
same as a failed call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Literal

from forecasting_tools.data_models.questions import DateQuestion, NumericQuestion
from forecasting_tools.helpers.prediction_extractor import PredictionExtractor

from betomcat.llm import LLMResult, OpenRouterClient
from betomcat.pool import ModelSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

QuestionKind = Literal["binary", "multiple_choice", "numeric"]


class ForecastParseError(Exception):
    """Raised when a model's response could not be parsed into a forecast."""


@dataclass(frozen=True)
class PromptFields:
    """Placeholders filled into `prompts/*.md` (string.Template `$name` style)."""

    title: str
    background: str
    resolution_criteria: str
    fine_print: str
    today: str
    close_time: str
    resolve_time: str
    briefing: str
    claims: str
    family_claims: str
    history: str
    units: str = ""
    bounds_text: str = ""
    options_list: str = ""


@dataclass(frozen=True)
class ModelResult:
    """One model's parsed forecast plus everything the ledger/comment need."""

    model_id: str
    rationale: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    value: float | dict[str, float] | list[float]


def render_prompt(kind: QuestionKind, fields: PromptFields) -> str:
    """Fill `prompts/{kind}.md` with `fields`."""
    template = Template((PROMPTS_DIR / f"{kind}.md").read_text(encoding="utf-8"))
    return template.substitute(
        title=fields.title,
        background=fields.background,
        resolution_criteria=fields.resolution_criteria,
        fine_print=fields.fine_print,
        today=fields.today,
        close_time=fields.close_time,
        resolve_time=fields.resolve_time,
        briefing=fields.briefing,
        claims=fields.claims,
        family_claims=fields.family_claims,
        history=fields.history,
        units=fields.units,
        bounds_text=fields.bounds_text,
        options_list=fields.options_list,
    )


def _to_model_result(
    model: ModelSpec, llm_result: LLMResult, value: object
) -> ModelResult:
    return ModelResult(
        model_id=model.id,
        rationale=llm_result.text,
        cost_usd=llm_result.cost_usd,
        tokens_in=llm_result.tokens_in,
        tokens_out=llm_result.tokens_out,
        value=value,  # type: ignore[arg-type]
    )


async def forecast_binary(
    model: ModelSpec, prompt: str, llm: OpenRouterClient, timeout: float
) -> ModelResult:
    """Call `model` and parse a binary probability from its response."""
    result = await llm.complete(model, prompt, timeout)
    try:
        probability = PredictionExtractor.extract_last_percentage_value(result.text)
    except ValueError as exc:
        raise ForecastParseError(f"{model.id}: {exc}") from exc
    return _to_model_result(model, result, probability)


async def forecast_multiple_choice(
    model: ModelSpec,
    prompt: str,
    llm: OpenRouterClient,
    timeout: float,
    options: list[str],
) -> ModelResult:
    """Call `model` and parse a per-option probability distribution."""
    result = await llm.complete(model, prompt, timeout)
    try:
        predicted = PredictionExtractor.extract_option_list_with_percentage_afterwards(
            result.text, options
        )
    except ValueError as exc:
        raise ForecastParseError(f"{model.id}: {exc}") from exc
    option_probs = {
        po.option_name: po.probability for po in predicted.predicted_options
    }
    return _to_model_result(model, result, option_probs)


async def forecast_numeric(
    model: ModelSpec,
    prompt: str,
    llm: OpenRouterClient,
    timeout: float,
    question: NumericQuestion | DateQuestion,
) -> ModelResult:
    """Call `model` and parse a CDF from its declared percentiles."""
    result = await llm.complete(model, prompt, timeout)
    try:
        extract_distribution = PredictionExtractor.extract_numeric_distribution_from_list_of_percentile_number_and_probability  # noqa: E501
        distribution = extract_distribution(result.text, question)
        cdf = [point.percentile for point in distribution.get_cdf()]
    except ValueError as exc:
        raise ForecastParseError(f"{model.id}: {exc}") from exc
    return _to_model_result(model, result, cdf)
