"""Tests for prompt rendering and per-model forecast parsing."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import pytest
from forecasting_tools.data_models.questions import NumericQuestion

from betomcat.forecast import (
    ForecastParseError,
    PromptFields,
    forecast_binary,
    forecast_multiple_choice,
    forecast_numeric,
    render_prompt,
)
from betomcat.llm import LLMResult
from betomcat.pool import ModelSpec

MODEL = ModelSpec(id="anthropic/claude-sonnet-5", tier="frontier", enabled=True)


@dataclass
class FakeLLM:
    """Stands in for `OpenRouterClient`: returns a canned response."""

    text: str
    cost_usd: float = 0.01
    tokens_in: int = 100
    tokens_out: int = 50

    async def complete(
        self, model: ModelSpec, prompt: str, timeout: float
    ) -> LLMResult:
        return LLMResult(
            text=self.text,
            cost_usd=self.cost_usd,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
        )


def _fields(**overrides: str) -> PromptFields:
    base = dict(
        title="Will X happen?",
        background="Some background.",
        resolution_criteria="Resolves YES if X.",
        fine_print="Fine print here.",
        today="2026-09-22",
        close_time="2026-09-23T00:00:00Z",
        resolve_time="2026-09-30T00:00:00Z",
        briefing="### Situation Summary\nnothing new",
        claims="- [support=0.80] some claim",
        family_claims="(none)",
        history="(none)",
        units="",
        bounds_text="",
        options_list="",
    )
    base.update(overrides)
    return PromptFields(**base)


def test_render_prompt_binary_fills_placeholders() -> None:
    text = render_prompt("binary", _fields())

    assert "Will X happen?" in text
    assert "Resolves YES if X." in text
    assert "Fine print here." in text
    assert "some claim" in text
    assert "Probability: ZZ%" in text


def test_render_prompt_multiple_choice_fills_placeholders() -> None:
    text = render_prompt("multiple_choice", _fields(options_list="Option A, Option B"))

    assert "Option A, Option B" in text
    assert "Will X happen?" in text


def test_render_prompt_numeric_fills_placeholders() -> None:
    text = render_prompt(
        "numeric",
        _fields(units="widgets", bounds_text="[0, 100] (closed lower, open upper)"),
    )

    assert "widgets" in text
    assert "[0, 100] (closed lower, open upper)" in text
    assert "Percentile 50:" in text


async def test_forecast_binary_parses_percentage() -> None:
    llm = FakeLLM(text="Reasoning...\n\nProbability: 37%")

    result = await forecast_binary(MODEL, "prompt", llm, timeout=10.0)

    assert result.value == pytest.approx(0.37)
    assert result.model_id == MODEL.id
    assert result.cost_usd == pytest.approx(0.01)


async def test_forecast_binary_parse_failure_raises() -> None:
    llm = FakeLLM(text="I refuse to give a number.")

    with pytest.raises(ForecastParseError):
        await forecast_binary(MODEL, "prompt", llm, timeout=10.0)


async def test_forecast_multiple_choice_parses_options() -> None:
    llm = FakeLLM(text="Reasoning...\n\nYes: 70%\nNo: 30%")

    result = await forecast_multiple_choice(
        MODEL, "prompt", llm, timeout=10.0, options=["Yes", "No"]
    )

    assert result.value == pytest.approx({"Yes": 0.7, "No": 0.3})


async def test_forecast_numeric_parses_percentiles_into_cdf() -> None:
    question = NumericQuestion(
        question_text="How many widgets?",
        upper_bound=100.0,
        lower_bound=0.0,
        open_upper_bound=True,
        open_lower_bound=True,
        zero_point=None,
    )
    text = "\n".join(
        f"Percentile {p}: {v}"
        for p, v in [
            (1, 5),
            (5, 10),
            (10, 15),
            (20, 25),
            (40, 40),
            (50, 50),
            (60, 60),
            (80, 75),
            (90, 85),
            (95, 90),
            (99, 95),
        ]
    )
    llm = FakeLLM(text=text)

    result = await forecast_numeric(
        MODEL, "prompt", llm, timeout=10.0, question=question
    )

    assert isinstance(result.value, list)
    assert len(result.value) == question.cdf_size
    cdf = list(result.value)
    assert all(0.0 <= p <= 1.0 for p in cdf)
    assert all(a <= b for a, b in pairwise(cdf))


async def test_forecast_numeric_parse_failure_raises() -> None:
    question = NumericQuestion(
        question_text="How many widgets?",
        upper_bound=100.0,
        lower_bound=0.0,
        open_upper_bound=True,
        open_lower_bound=True,
        zero_point=None,
    )
    llm = FakeLLM(text="no percentiles here")

    with pytest.raises(ForecastParseError):
        await forecast_numeric(MODEL, "prompt", llm, timeout=10.0, question=question)
