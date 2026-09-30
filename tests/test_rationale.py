"""Tests for the synthesized-rationale call and the forecast description helper."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from forecasting_tools.data_models.questions import (
    BinaryQuestion,
    DateQuestion,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
)

from betomcat.comment import ModelForecastInfo
from betomcat.llm import LLMError, LLMResult
from betomcat.pool import ModelSpec
from betomcat.rationale import (
    MAX_RATIONALE_WORDS,
    RATIONALE_MODEL,
    build_prompt,
    describe_forecast,
    finalize_rationale,
    synthesize_rationale,
)

CDF_POINTS = 201


def _words(count: int) -> str:
    return " ".join(f"word{i}" for i in range(count))


def _binary_question() -> BinaryQuestion:
    return BinaryQuestion(
        question_text="Will X happen by 2026-12-31?",
        resolution_criteria="Resolves YES if the agency publishes X.",
    )


def _mc_question() -> MultipleChoiceQuestion:
    return MultipleChoiceQuestion(
        question_text="Who wins?",
        options=["Alpha", "Beta", "Gamma"],
    )


def _numeric_question(**overrides: object) -> NumericQuestion:
    fields: dict[str, object] = dict(
        question_text="How many widgets?",
        lower_bound=0.0,
        upper_bound=100.0,
        open_lower_bound=False,
        open_upper_bound=False,
        zero_point=None,
    )
    fields.update(overrides)
    return NumericQuestion(**fields)  # type: ignore[arg-type]


def _cdf(start: float = 0.0, end: float = 1.0) -> list[float]:
    """A CDF rising linearly from `start` to `end` over `CDF_POINTS` points."""
    return [start + (end - start) * i / (CDF_POINTS - 1) for i in range(CDF_POINTS)]


def _forecasts() -> list[ModelForecastInfo]:
    return [
        ModelForecastInfo(
            "model-a",
            0.65,
            "summary a",
            "Model A saw the 2026-09-01 vote slip.\n\nSecond paragraph of A.",
        ),
        ModelForecastInfo(
            "model-b",
            0.40,
            "summary b",
            "Model B anchored on a 30% base rate.\nPercentile line B.",
        ),
    ]


# -- describe_forecast -------------------------------------------------------


@pytest.mark.parametrize(
    ("probability", "expected"),
    [
        (0.724, "72.4% probability of YES"),
        (0.5, "50% probability of YES"),
        (0.01, "1% probability of YES"),
    ],
)
def test_describe_forecast_binary(probability: float, expected: str) -> None:
    assert describe_forecast("binary", probability, _binary_question()) == expected


def test_describe_forecast_multiple_choice_lists_top_three_and_the_rest() -> None:
    value = {"A": 0.06, "B": 0.5, "C": 0.3, "D": 0.04, "E": 0.1}

    text = describe_forecast("multiple_choice", value, _mc_question())

    assert text == "B 50%, C 30%, E 10% (2 other options share 10%)"


def test_describe_forecast_multiple_choice_with_three_options_has_no_remainder() -> (
    None
):
    value = {"Alpha": 0.2, "Beta": 0.7, "Gamma": 0.1}

    text = describe_forecast("multiple_choice", value, _mc_question())

    assert text == "Beta 70%, Alpha 20%, Gamma 10%"


def test_describe_forecast_numeric_closed_bounds_reads_percentiles() -> None:
    text = describe_forecast("numeric", _cdf(), _numeric_question())

    assert text == "median 50, 10th to 90th percentile range 10 to 90"


def test_describe_forecast_numeric_appends_unit() -> None:
    question = _numeric_question(unit_of_measure="widgets")

    text = describe_forecast("numeric", _cdf(), question)

    assert text.endswith("range 10 to 90 (widgets)")


def test_describe_forecast_numeric_open_lower_bound_mass_reads_as_below() -> None:
    """20% of the mass sits below the lower bound, so the 10th percentile does."""
    question = _numeric_question(open_lower_bound=True)

    text = describe_forecast("numeric", _cdf(start=0.2), question)

    assert text == (
        "median 37.5, 10th to 90th percentile range below the lower bound (0) to 87.5"
    )


def test_describe_forecast_numeric_open_upper_bound_mass_reads_as_above() -> None:
    """15% of the mass sits above the upper bound, so the 90th percentile does."""
    question = _numeric_question(open_upper_bound=True)

    text = describe_forecast("numeric", _cdf(end=0.85), question)

    assert text == (
        "median 58.8, 10th to 90th percentile range 11.8 to above the upper bound (100)"
    )


def test_describe_forecast_numeric_log_scale_uses_zero_point() -> None:
    question = _numeric_question(lower_bound=1.0, upper_bound=100.0, zero_point=0.0)

    text = describe_forecast("numeric", _cdf(), question)

    assert text == "median 10, 10th to 90th percentile range 1.58 to 63.1"


def test_describe_forecast_numeric_large_values_have_no_exponent() -> None:
    question = _numeric_question(lower_bound=0.0, upper_bound=2_000_000.0)

    text = describe_forecast("numeric", _cdf(), question)

    assert (
        text == "median 1,000,000, 10th to 90th percentile range 200,000 to 1,800,000"
    )


def test_describe_forecast_date_renders_dates() -> None:
    question = DateQuestion(
        question_text="When?",
        lower_bound=datetime(2026, 1, 1, tzinfo=UTC),
        upper_bound=datetime(2027, 1, 1, tzinfo=UTC),
        open_lower_bound=False,
        open_upper_bound=False,
    )

    text = describe_forecast("numeric", _cdf(), question)

    assert text == (
        "median 2026-07-02, 10th to 90th percentile range 2026-02-06 to 2026-11-25"
    )


# -- finalize_rationale ------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n ", _words(39)])
def test_finalize_rationale_rejects_short_replies(text: str) -> None:
    assert finalize_rationale(text) is None


def test_finalize_rationale_keeps_a_reply_within_the_cap_as_written() -> None:
    reply = f"{_words(30)}\n\n{_words(30)}"

    assert finalize_rationale(f"  {reply}\n") == reply


def test_finalize_rationale_truncates_over_long_replies_to_the_word_cap() -> None:
    result = finalize_rationale(_words(400))

    assert result is not None
    assert len(result.split()) == MAX_RATIONALE_WORDS
    assert result.endswith("word299...")


def test_finalize_rationale_does_not_stack_periods_on_the_ellipsis() -> None:
    words = ["word."] * 400

    result = finalize_rationale(" ".join(words))

    assert result is not None
    assert result.endswith("word...")
    assert not result.endswith("....")


# -- build_prompt ------------------------------------------------------------


def test_build_prompt_carries_every_full_rationale_and_the_described_forecast() -> None:
    forecasts = _forecasts()

    prompt = build_prompt(_binary_question(), "binary", 0.525, forecasts)

    for mf in forecasts:
        assert mf.rationale in prompt
    assert "52.5% probability of YES" in prompt
    assert "65% probability of YES" in prompt
    assert "40% probability of YES" in prompt
    assert "Will X happen by 2026-12-31?" in prompt
    assert "Resolves YES if the agency publishes X." in prompt
    assert "300" in prompt


def test_build_prompt_omits_resolution_criteria_when_absent() -> None:
    question = _binary_question()
    question.resolution_criteria = None

    prompt = build_prompt(question, "binary", 0.5, _forecasts())

    assert "Resolution criteria" not in prompt


def test_build_prompt_describes_numeric_forecasts_from_the_cdf() -> None:
    forecasts = [ModelForecastInfo("model-a", _cdf(), "s", "A numeric rationale.")]

    prompt = build_prompt(_numeric_question(), "numeric", _cdf(), forecasts)

    assert "median 50, 10th to 90th percentile range 10 to 90" in prompt


# -- synthesize_rationale ----------------------------------------------------


@dataclass
class FakeLLM:
    """Stands in for `OpenRouterClient`: returns, raises or hangs on demand."""

    text: str = ""
    error: Exception | None = None
    hang: bool = False
    calls: list[tuple[ModelSpec, str, float]] = field(default_factory=list)

    async def complete(
        self, model: ModelSpec, prompt: str, timeout: float
    ) -> LLMResult:
        self.calls.append((model, prompt, timeout))
        if self.hang:
            await asyncio.sleep(10)
        if self.error is not None:
            raise self.error
        return LLMResult(text=self.text, cost_usd=0.004, tokens_in=900, tokens_out=400)


async def _synthesize(
    llm: FakeLLM, question: MetaculusQuestion | None = None, timeout: float = 30.0
) -> str | None:
    return await synthesize_rationale(
        question or _binary_question(),
        "binary",
        0.525,
        _forecasts(),
        llm,  # type: ignore[arg-type]
        timeout,
    )


async def test_synthesize_rationale_returns_the_paragraph() -> None:
    reply = _words(120)
    llm = FakeLLM(text=reply)

    result = await _synthesize(llm, timeout=12.0)

    assert result == reply
    assert len(llm.calls) == 1
    model, prompt, timeout = llm.calls[0]
    assert model is RATIONALE_MODEL
    assert timeout == 12.0
    assert "Model A saw the 2026-09-01 vote slip." in prompt


def test_rationale_model_is_the_cheap_funded_luna_spec() -> None:
    assert RATIONALE_MODEL.id == "openai/gpt-6-luna"
    assert RATIONALE_MODEL.tier == "cheap"
    assert RATIONALE_MODEL.key == "funded"
    assert RATIONALE_MODEL.max_tokens == 1500
    assert RATIONALE_MODEL.reasoning == {"effort": "low"}


async def test_synthesize_rationale_llm_error_returns_none() -> None:
    llm = FakeLLM(error=LLMError("openai/gpt-6-luna: HTTP 500"))

    assert await _synthesize(llm) is None


async def test_synthesize_rationale_timeout_returns_none() -> None:
    llm = FakeLLM(hang=True, text=_words(120))

    assert await _synthesize(llm, timeout=0.01) is None


async def test_synthesize_rationale_short_reply_returns_none() -> None:
    llm = FakeLLM(text=_words(39))

    assert await _synthesize(llm) is None


async def test_synthesize_rationale_over_long_reply_is_truncated() -> None:
    llm = FakeLLM(text=_words(450))

    result = await _synthesize(llm)

    assert result is not None
    assert len(result.split()) == MAX_RATIONALE_WORDS
    assert result.endswith("...")
