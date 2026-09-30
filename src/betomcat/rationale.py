"""One synthesized rationale for the posted comment (contracts.md s E).

A single cheap-tier call (`RATIONALE_MODEL`, spec s5: no frontier spend outside
the forecast calls) writes one plain-text paragraph of at most
`MAX_RATIONALE_WORDS` words from both models' full rationales and the
submitted number. The model is told to use only facts stated in those
rationales. Every failure mode returns `None`, and the caller then posts the
per-model summary comment instead, so a comment is never missing.

The repo and its Actions logs are public: nothing here logs a rationale, the
synthesized text, or any probability.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from bisect import bisect_left
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import cast

from forecasting_tools.data_models.questions import (
    DateQuestion,
    MetaculusQuestion,
    NumericQuestion,
)

from betomcat.comment import ModelForecastInfo
from betomcat.forecast import QuestionKind
from betomcat.llm import LLMError, OpenRouterClient
from betomcat.pool import ModelSpec

logger = logging.getLogger(__name__)

RATIONALE_MODEL = ModelSpec(
    id="openai/gpt-6-luna",
    tier="cheap",
    enabled=True,
    max_tokens=1500,
    reasoning={"effort": "low"},
    key="funded",
)

MAX_RATIONALE_WORDS = 300
MIN_RATIONALE_WORDS = 40
_MC_TOP_OPTIONS = 3
_SIGNIFICANT_DIGITS = 3

ForecastValue = float | dict[str, float] | list[float]

_INSTRUCTIONS = f"""\
You are writing the explanation that is posted next to a forecast already \
submitted on Metaculus. Below you get the question, the submitted forecast, and \
the full reasoning of the forecasts it was built from. Write it as one argument \
for the submitted forecast, in one voice.

Rules:
- Plain text only: one to three short paragraphs, at most {MAX_RATIONALE_WORDS} \
words in total. No headings, no bullet or numbered lists, no markdown, no \
preamble.
- Open with the submitted forecast, worded as given below.
- Then cover, in whatever order reads best: the base rate or status quo the \
forecast anchors on; the two or three pieces of evidence that moved it, with \
dates where the reasoning gives them; where the forecasts disagreed, if they \
did; and what would change the forecast.
- Use ONLY facts, numbers, dates and sources stated in the reasoning below. \
Never add a claim, number or source of your own.
- If you must tell the forecasts apart, say "one forecast" and "the other"."""


def _percent(probability: float) -> str:
    """`0.724` -> `"72.4%"`, `0.5` -> `"50%"` (one decimal, trailing zero dropped)."""
    text = f"{probability * 100:.1f}"
    return f"{text.removesuffix('.0')}%"


def _format_number(value: float) -> str:
    """`value` to three significant digits, with thousands separators, no exponent."""
    if value == 0:
        return "0"
    decimals = _SIGNIFICANT_DIGITS - 1 - math.floor(math.log10(abs(value)))
    text = f"{round(value, decimals):,.{max(decimals, 0)}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _format_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


def _nominal_location(
    cdf_location: float, lower: float, upper: float, zero_point: float | None
) -> float:
    """Map a CDF x-location in [0, 1] to the question's value scale.

    Mirrors `forecasting_tools`' `NumericDistribution._cdf_location_to_nominal_location`
    (linear scale, or log-like scale when the question has a `zero_point`).
    """
    if zero_point is None:
        return lower + (upper - lower) * cdf_location
    ratio = (upper - zero_point) / (lower - zero_point)
    return lower + (upper - lower) * (math.pow(ratio, cdf_location) - 1) / (ratio - 1)


def _quantile_text(
    cdf: Sequence[float],
    quantile: float,
    to_value: Callable[[float], float],
    fmt: Callable[[float], str],
) -> str:
    """The value at `quantile` of `cdf`, read by linear interpolation.

    `cdf[0]` is the mass below the lower bound and `1 - cdf[-1]` the mass above
    the upper bound (both zero for a closed bound); a quantile that falls inside
    that outside mass is reported as beyond the bound rather than as a number.
    """
    if quantile <= cdf[0]:
        return f"below the lower bound ({fmt(to_value(0.0))})"
    if quantile > cdf[-1]:
        return f"above the upper bound ({fmt(to_value(1.0))})"
    index = bisect_left(cdf, quantile)
    low, high = cdf[index - 1], cdf[index]
    cdf_location = (index - 1 + (quantile - low) / (high - low)) / (len(cdf) - 1)
    return fmt(to_value(cdf_location))


def _describe_cdf(cdf: list[float], question: MetaculusQuestion) -> str:
    assert isinstance(question, NumericQuestion | DateQuestion)
    fmt: Callable[[float], str]
    if isinstance(question, DateQuestion):
        lower, upper = (
            question.lower_bound.timestamp(),
            question.upper_bound.timestamp(),
        )
        fmt, unit = _format_date, ""
    else:
        lower, upper = question.lower_bound, question.upper_bound
        fmt, unit = _format_number, question.unit_of_measure or ""

    def to_value(cdf_location: float) -> float:
        return _nominal_location(cdf_location, lower, upper, question.zero_point)

    median, p10, p90 = (_quantile_text(cdf, q, to_value, fmt) for q in (0.5, 0.1, 0.9))
    text = f"median {median}, 10th to 90th percentile range {p10} to {p90}"
    return f"{text} ({unit})" if unit else text


def describe_forecast(
    kind: QuestionKind, value: ForecastValue, question: MetaculusQuestion
) -> str:
    """Describe a forecast in words the synthesis model can repeat.

    Args:
        kind: The question kind.
        value: A probability (binary), an `{option: probability}` map (multiple
            choice), or the CDF y-values at the question's evenly spaced
            evaluation points (numeric, discrete and date).
        question: The question; its bounds, `zero_point` and unit place a CDF
            on the value axis.

    Returns:
        E.g. `"72.4% probability of YES"`, `"Alpha 52%, Beta 30%, Gamma 10%
        (2 other options share 8%)"`, or `"median 12.3, 10th to 90th percentile
        range 8.11 to 19.4"`. An open bound with mass beyond it reads "below the
        lower bound (X)" or "above the upper bound (X)" where a percentile
        falls outside it.
    """
    if kind == "binary":
        return f"{_percent(cast(float, value))} probability of YES"
    if kind == "multiple_choice":
        ranked = sorted(
            cast(dict[str, float], value).items(), key=lambda item: -item[1]
        )
        top = ", ".join(
            f"{option} {_percent(p)}" for option, p in ranked[:_MC_TOP_OPTIONS]
        )
        rest = ranked[_MC_TOP_OPTIONS:]
        if not rest:
            return top
        share = _percent(sum(p for _, p in rest))
        return f"{top} ({len(rest)} other options share {share})"
    return _describe_cdf(cast(list[float], value), question)


def build_prompt(
    question: MetaculusQuestion,
    kind: QuestionKind,
    submitted: ForecastValue,
    model_forecasts: Sequence[ModelForecastInfo],
) -> str:
    """The synthesis prompt: instructions, question, submitted forecast, rationales."""
    parts = [_INSTRUCTIONS, f"## Question\n\n{question.question_text}"]
    if question.resolution_criteria:
        parts.append(f"## Resolution criteria\n\n{question.resolution_criteria}")
    parts.append(
        f"## Submitted forecast\n\n{describe_forecast(kind, submitted, question)}"
    )
    for index, mf in enumerate(model_forecasts, start=1):
        described = describe_forecast(kind, mf.value, question)
        parts.append(f"## Forecast {index}: {described}\n\n{mf.rationale}")
    return "\n\n".join(parts)


def finalize_rationale(text: str) -> str | None:
    """Validate a synthesis reply; `None` if unusable, else within the word cap.

    A reply under `MIN_RATIONALE_WORDS` words is rejected. One over
    `MAX_RATIONALE_WORDS` is cut after that many words and ends with "...".
    """
    text = text.strip()
    words = list(re.finditer(r"\S+", text))
    if len(words) < MIN_RATIONALE_WORDS:
        return None
    if len(words) <= MAX_RATIONALE_WORDS:
        return text
    kept = text[: words[MAX_RATIONALE_WORDS - 1].end()]
    return f"{kept.rstrip(' .,;:')}..."


async def synthesize_rationale(
    question: MetaculusQuestion,
    kind: QuestionKind,
    submitted: ForecastValue,
    model_forecasts: Sequence[ModelForecastInfo],
    llm: OpenRouterClient,
    timeout: float,
) -> str | None:
    """Write one rationale for the submitted forecast, or `None` to fall back.

    Args:
        question: The forecast question.
        kind: The question kind.
        submitted: The reconciled value that was submitted.
        model_forecasts: Each model's id, forecast value and full rationale.
        llm: The OpenRouter client.
        timeout: Wall-clock seconds allowed for the whole call.

    Returns:
        The rationale (at most `MAX_RATIONALE_WORDS` words), or `None` when the
        call failed or timed out, or the reply was empty or under
        `MIN_RATIONALE_WORDS` words.
    """
    prompt = build_prompt(question, kind, submitted, model_forecasts)
    try:
        result = await asyncio.wait_for(
            llm.complete(RATIONALE_MODEL, prompt, timeout), timeout
        )
    # ValueError: `llm.complete` does not wrap a non-JSON 200 body.
    except (LLMError, TimeoutError, ValueError) as exc:
        logger.warning("rationale synthesis call failed: %s", type(exc).__name__)
        return None
    rationale = finalize_rationale(result.text)
    if rationale is None:
        logger.warning("rationale synthesis reply too short")
    return rationale
