"""The referee gate's parts: the Jev client, the class rules, the query writer.

When the two drawn models diverge, `pipeline.py` asks Jev (TypeSafe, not an LLM)
why: one `choice` question over the question, its resolution criteria and both
rationales. Two classes are fixable by a second round, `stale_or_missing_
information` and `misread_resolution_criteria`; for those one cheap-tier call
(`REFEREE_MODEL`) writes 1-3 targeted search queries plus a one-line diagnosis.
If that call fails, a fixed diagnosis sentence per class stands in and no extra
queries are sent. The referee never touches the weighted-average arithmetic; it
only decides whether a second round of forecasts happens (HANDOFF s5.1).

Every failure here fails open: `classify_disagreement` returns `None` and
`write_targeted_queries` returns the fixed plan. The Actions logs are public, so
nothing in this module logs a question, rationale, query, diagnosis or number --
only class names and exception type names.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import httpx
import orjson

from betomcat.llm import LLMError, OpenRouterClient
from betomcat.pool import ModelSpec

logger = logging.getLogger(__name__)

JEV_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
JEV_TIMEOUT = 20.0
_JEV_ATTEMPTS = 2
_DEFAULT_RETRY_AFTER = 1.0
_MAX_RETRY_AFTER = 5.0
_CLASS_QUESTION_ID = "disagreement"

DisagreementClass = Literal[
    "stale_or_missing_information",
    "misread_resolution_criteria",
    "different_base_rate",
    "genuine_uncertainty",
    "other",
]

DISAGREEMENT_CRITERIA: dict[str, str] = {
    "stale_or_missing_information": (
        "One forecaster relied on outdated information, or missed a recent "
        "development or fact the other had, so newer or more targeted research "
        "could close the gap."
    ),
    "misread_resolution_criteria": (
        "The forecasters read the resolution criteria or fine print differently, "
        "and at least one reading is wrong or overlooks an edge case."
    ),
    "different_base_rate": (
        "They agree on the facts and the criteria but anchored on different "
        "reference classes or base rates."
    ),
    "genuine_uncertainty": (
        "They saw the same facts and read the criteria the same way, and simply "
        "weigh an uncertain outcome differently; no research would settle it."
    ),
    "other": "None of the other categories fits.",
}

RERUN_CLASSES: frozenset[DisagreementClass] = frozenset(
    {"stale_or_missing_information", "misread_resolution_criteria"}
)

FIXED_DIAGNOSIS: dict[DisagreementClass, str] = {
    "stale_or_missing_information": (
        "The earlier research may have missed recent or relevant developments; "
        "weigh the newly gathered evidence carefully."
    ),
    "misread_resolution_criteria": (
        "The resolution criteria or fine print may have been misread; read them "
        "literally, edge cases included, before forecasting."
    ),
}

_CLASS_INSTRUCTIONS = (
    "Two forecasters answered the same forecasting question with clearly different "
    "probabilities. Given the question, its resolution criteria and each "
    "forecaster's reasoning, choose the single category that best explains why "
    "they disagree."
)

# Cheap tier (spec s5: no frontier spend outside the two forecast calls); the
# same Luna model as `rationale.RATIONALE_MODEL`, with a smaller reply budget.
REFEREE_MODEL = ModelSpec(
    id="openai/gpt-6-luna",
    tier="cheap",
    enabled=True,
    max_tokens=1000,
    reasoning={"effort": "low"},
    key="funded",
)
QUERY_WRITER_TIMEOUT = 60.0
# IW rejects more than 3 extra queries, an empty one, or one over 200 characters.
MAX_QUERIES = 3
MAX_QUERY_CHARS = 200
MAX_DIAGNOSIS_CHARS = 300


class JevError(Exception):
    """Raised when a Jev request fails after its retry or the answer is unusable."""


@dataclass(frozen=True)
class ChoiceQuestion:
    """A single-choice Jev question (TypeSafe API reference, Choice)."""

    instructions: str
    criteria: dict[str, str]

    def to_json(self) -> dict[str, object]:
        """Render this question in the `/v1/systemone` request shape."""
        return {
            "type": "choice",
            "instructions": self.instructions,
            "criteria": self.criteria,
        }


class JevClient:
    """Async client for one `/v1/systemone` call, with one retry on 429/5xx."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_JEV_MODEL,
        timeout: float = JEV_TIMEOUT,
        base_url: str = JEV_URL,
    ) -> None:
        """Args:
        api_key: Bearer token for the `Authorization` header. Never logged.
        model: The Jev model to request.
        timeout: Per-request timeout in seconds.
        base_url: The `/v1/systemone` endpoint url.
        """
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._base_url = base_url

    async def ask(
        self, state: object, questions: dict[str, ChoiceQuestion]
    ) -> dict[str, dict[str, object]]:
        """Evaluate `questions` against `state` in a single request.

        Args:
            state: The content to evaluate.
            questions: Caller-chosen question id -> question.

        Returns:
            The response's `answers` map, keyed by the same question ids.

        Raises:
            JevError: On a request failure after one retry (429 or 5xx, honouring
                a capped `retry-after`), or a missing or malformed `answers`.
        """
        body = {
            "state": state,
            "model": self._model,
            "questions": {key: q.to_json() for key, q in questions.items()},
        }
        response = await self._post_with_retry(body)
        try:
            answers = response.json()["answers"]
        except (KeyError, TypeError, ValueError) as exc:
            raise JevError("jev response malformed: missing answers") from exc
        if not isinstance(answers, dict):
            raise JevError("jev response malformed: answers is not an object")
        return answers

    async def _post_with_retry(self, body: dict[str, object]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in range(_JEV_ATTEMPTS):
                try:
                    response = await client.post(
                        self._base_url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=body,
                    )
                except httpx.HTTPError as exc:
                    raise JevError(f"jev request failed: {type(exc).__name__}") from exc
                if response.status_code == 200:
                    return response
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == _JEV_ATTEMPTS - 1:
                    raise JevError(f"jev request failed: {response.status_code}")
                await asyncio.sleep(_retry_after_seconds(response))
        raise JevError("jev request failed")  # pragma: no cover - loop always returns


def _retry_after_seconds(response: httpx.Response) -> float:
    """`Retry-After` in seconds, defaulting to 1.0 and capped at `_MAX_RETRY_AFTER`."""
    header = response.headers.get("retry-after")
    if header is None:
        return _DEFAULT_RETRY_AFTER
    try:
        return min(max(0.0, float(header)), _MAX_RETRY_AFTER)
    except ValueError:
        return _DEFAULT_RETRY_AFTER


def jev_client_from_env() -> JevClient | None:
    """A `JevClient` from `TYPESAFE_API_KEY` / `JEV_MODEL`, or `None` if no key.

    `None` switches the referee off: no provisional step, no re-run, today's
    behaviour.
    """
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return None
    return JevClient(api_key, os.environ.get("JEV_MODEL") or DEFAULT_JEV_MODEL)


async def classify_disagreement(
    jev: JevClient,
    question_text: str,
    resolution_text: str,
    rationales: Sequence[str],
) -> DisagreementClass | None:
    """Ask Jev why the two forecasts disagree.

    Args:
        jev: The Jev client.
        question_text: The question title.
        resolution_text: The resolution criteria (and fine print, if any).
        rationales: Each drawn model's full rationale.

    Returns:
        One of `DISAGREEMENT_CRITERIA`'s classes, or `None` when Jev failed or
        answered with something else (the caller then treats the referee as
        absent).
    """
    state = {
        "question": question_text,
        "resolution_criteria": resolution_text,
        "rationales": list(rationales),
    }
    question = ChoiceQuestion(_CLASS_INSTRUCTIONS, dict(DISAGREEMENT_CRITERIA))
    try:
        answers = await jev.ask(state, {_CLASS_QUESTION_ID: question})
        choice = answers[_CLASS_QUESTION_ID]["choice"]
    except (JevError, KeyError, TypeError) as exc:
        logger.warning("referee: jev classification failed (%s)", type(exc).__name__)
        return None
    if isinstance(choice, str) and choice in DISAGREEMENT_CRITERIA:
        return cast(DisagreementClass, choice)
    logger.warning("referee: jev answered outside the categories")
    return None


@dataclass(frozen=True)
class RerunPlan:
    """What the second research is told: extra queries and a one-line diagnosis."""

    queries: list[str]
    diagnosis: str


def fixed_plan(label: DisagreementClass) -> RerunPlan:
    """The fallback plan for a rerun class: no extra queries, a fixed sentence."""
    return RerunPlan([], FIXED_DIAGNOSIS[label])


def build_query_prompt(
    question_text: str,
    resolution_text: str,
    label: DisagreementClass,
    rationales: Sequence[str],
) -> str:
    """The query-writer prompt: the question, the suspected cause, both rationales."""
    parts = [
        "Two forecasters disagreed about the question below, and a classifier "
        f"judged the cause to be: {label.replace('_', ' ')}. Help a second round "
        "of research fix it.\n\n"
        "Reply with one JSON object and nothing else:\n"
        '{"diagnosis": "...", "queries": ["...", "..."]}\n\n'
        "- diagnosis: one sentence of at most 40 words naming the specific "
        "information that looks stale or missing, or the specific clause of the "
        "resolution criteria that looks misread. Do not state either forecaster's "
        "probability and do not repeat their reasoning: the sentence is shown to "
        "both forecasters, who must not see each other's work.\n"
        f"- queries: 1 to {MAX_QUERIES} short news search queries, each at most "
        f"{MAX_QUERY_CHARS} characters, most likely to surface that information.",
        f"## Question\n\n{question_text}",
        f"## Resolution criteria\n\n{resolution_text}",
    ]
    parts.extend(
        f"## Forecaster {index} reasoning\n\n{text}"
        for index, text in enumerate(rationales, start=1)
    )
    return "\n\n".join(parts)


def parse_query_reply(text: str) -> RerunPlan | None:
    """Parse the query writer's JSON reply; `None` unless it has both parts.

    Queries are whitespace-collapsed, cut to `MAX_QUERY_CHARS`, de-duplicated and
    limited to `MAX_QUERIES` so IW never rejects them; the diagnosis is collapsed
    to one line of at most `MAX_DIAGNOSIS_CHARS`.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = orjson.loads(text[start : end + 1])
    except orjson.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    diagnosis, raw_queries = data.get("diagnosis"), data.get("queries")
    if not isinstance(diagnosis, str) or not isinstance(raw_queries, list):
        return None
    diagnosis = " ".join(diagnosis.split())[:MAX_DIAGNOSIS_CHARS]
    queries = [
        " ".join(item.split())[:MAX_QUERY_CHARS].rstrip()
        for item in raw_queries
        if isinstance(item, str)
    ]
    queries = list(dict.fromkeys(q for q in queries if q))[:MAX_QUERIES]
    if not diagnosis or not queries:
        return None
    return RerunPlan(queries, diagnosis)


async def write_targeted_queries(
    llm: OpenRouterClient,
    question_text: str,
    resolution_text: str,
    label: DisagreementClass,
    rationales: Sequence[str],
    timeout: float,
) -> RerunPlan:
    """One cheap-tier call for 1-3 queries and a diagnosis; the fixed plan on failure.

    Args:
        llm: The OpenRouter client.
        question_text: The question title.
        resolution_text: The resolution criteria (and fine print, if any).
        label: The rerun class Jev chose.
        rationales: Each drawn model's full rationale.
        timeout: Wall-clock seconds allowed for the call.

    Returns:
        The model's plan, or `fixed_plan(label)` when the call failed, timed out
        or its reply was unusable.
    """
    prompt = build_query_prompt(question_text, resolution_text, label, rationales)
    try:
        result = await asyncio.wait_for(
            llm.complete(REFEREE_MODEL, prompt, timeout), timeout
        )
    # ValueError: `llm.complete` does not wrap a non-JSON 200 body.
    except (LLMError, TimeoutError, ValueError) as exc:
        logger.warning("referee: query writer failed (%s)", type(exc).__name__)
        return fixed_plan(label)
    plan = parse_query_reply(result.text)
    if plan is None:
        logger.warning("referee: query writer reply unusable")
        return fixed_plan(label)
    return plan


def render_rerun_note(diagnosis: str) -> str:
    """The note appended to the re-forecast briefing; never carries a rationale."""
    return (
        "### Second round\n"
        "An earlier round of forecasts on this question disagreed, and the research "
        f"above was refreshed to look into why. Likely cause: {diagnosis}"
    )
