"""Tests for the referee gate's parts: Jev client, class rules, query writer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat.llm import LLMError, LLMResult
from betomcat.rationale import RATIONALE_MODEL
from betomcat.referee import (
    DEFAULT_JEV_MODEL,
    DISAGREEMENT_CRITERIA,
    FIXED_DIAGNOSIS,
    JEV_URL,
    REFEREE_MODEL,
    RERUN_CLASSES,
    JevClient,
    JevError,
    RerunPlan,
    _retry_after_seconds,
    build_query_prompt,
    classify_disagreement,
    fixed_plan,
    jev_client_from_env,
    parse_query_reply,
    render_rerun_note,
    write_targeted_queries,
)

RATIONALES = ["Rationale of the first forecaster.", "Rationale of the second."]


def _answer(choice: str) -> dict[str, object]:
    return {
        "answers": {
            "disagreement": {
                "choice": choice,
                "probabilities": {choice: 0.8},
                "confidence": 0.9,
            }
        }
    }


async def _classify(client: JevClient) -> str | None:
    return await classify_disagreement(
        client, "Will X happen?", "Resolves YES if X.", RATIONALES
    )


# -- the class rules -----------------------------------------------------------


def test_only_stale_information_and_misread_criteria_trigger_a_rerun() -> None:
    assert {
        "stale_or_missing_information",
        "misread_resolution_criteria",
    } == RERUN_CLASSES
    assert list(DISAGREEMENT_CRITERIA) == [
        "stale_or_missing_information",
        "misread_resolution_criteria",
        "different_base_rate",
        "genuine_uncertainty",
        "other",
    ]
    assert set(FIXED_DIAGNOSIS) == RERUN_CLASSES


def test_the_query_writer_model_is_luna_but_not_the_rationale_model() -> None:
    assert REFEREE_MODEL.id == "openai/gpt-6-luna"
    assert REFEREE_MODEL != RATIONALE_MODEL


# -- the Jev client ------------------------------------------------------------


async def test_classify_sends_the_systemone_request_and_returns_the_choice(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=JEV_URL, method="POST", json=_answer("misread_resolution_criteria")
    )

    label = await _classify(JevClient("jev_test_key"))

    assert label == "misread_resolution_criteria"
    (request,) = httpx_mock.get_requests()
    assert request.headers["Authorization"] == "Bearer jev_test_key"
    body = orjson.loads(request.content)
    assert body["model"] == DEFAULT_JEV_MODEL == "jev-latest"
    assert body["state"] == {
        "question": "Will X happen?",
        "resolution_criteria": "Resolves YES if X.",
        "rationales": RATIONALES,
    }
    assert list(body["questions"]) == ["disagreement"]
    question = body["questions"]["disagreement"]
    assert question["type"] == "choice"
    assert question["criteria"] == DISAGREEMENT_CRITERIA
    assert "Two forecasters" in question["instructions"]


async def test_a_429_is_retried_once_after_its_retry_after(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=JEV_URL, status_code=429, headers={"retry-after": "0"})
    httpx_mock.add_response(url=JEV_URL, json=_answer("genuine_uncertainty"))

    assert await _classify(JevClient("k")) == "genuine_uncertainty"
    assert len(httpx_mock.get_requests()) == 2


async def test_a_second_5xx_fails_open_to_none_after_exactly_two_requests(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=JEV_URL, status_code=503, headers={"retry-after": "0"}, is_reusable=True
    )

    assert await _classify(JevClient("k")) is None
    assert len(httpx_mock.get_requests()) == 2


async def test_a_4xx_is_not_retried(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=JEV_URL, status_code=401, text="bad key")

    assert await _classify(JevClient("k")) is None
    assert len(httpx_mock.get_requests()) == 1


async def test_a_network_error_fails_open_to_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))

    assert await _classify(JevClient("k")) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"nothing": "here"},
        {"answers": []},
        {"answers": {}},
        {"answers": {"disagreement": {"probabilities": {}}}},
        {"answers": {"disagreement": "stale_or_missing_information"}},
        _answer("not_a_category"),
    ],
    ids=[
        "no-answers",
        "answers-not-an-object",
        "question-missing",
        "choice-missing",
        "answer-not-an-object",
        "choice-outside-the-categories",
    ],
)
async def test_an_unusable_answer_fails_open_to_none(
    httpx_mock: HTTPXMock, payload: dict[str, object]
) -> None:
    httpx_mock.add_response(url=JEV_URL, json=payload)

    assert await _classify(JevClient("k")) is None


async def test_a_non_json_200_raises_jev_error_from_ask(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=JEV_URL, text="<html>")

    with pytest.raises(JevError):
        await JevClient("k").ask({}, {})


def test_retry_after_is_capped_and_defaults_to_one_second() -> None:
    def seconds(headers: dict[str, str]) -> float:
        return _retry_after_seconds(httpx.Response(429, headers=headers))

    assert seconds({"retry-after": "3"}) == 3.0
    assert seconds({"retry-after": "3600"}) == 5.0
    assert seconds({"retry-after": "soon"}) == 1.0
    assert seconds({}) == 1.0


def test_jev_client_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_MODEL", raising=False)
    assert jev_client_from_env() is None

    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    assert jev_client_from_env() is None

    monkeypatch.setenv("TYPESAFE_API_KEY", "jev_env_key")
    client = jev_client_from_env()
    assert client is not None
    assert client._api_key == "jev_env_key"
    assert client._model == "jev-latest"

    monkeypatch.setenv("JEV_MODEL", "jev-next")
    client = jev_client_from_env()
    assert client is not None
    assert client._model == "jev-next"


# -- the query writer ----------------------------------------------------------


def test_parse_query_reply_cleans_what_iw_would_reject() -> None:
    long_query = "q" * 250
    reply = (
        "```json\n"
        + orjson.dumps(
            {
                "diagnosis": "  The vote\n date moved.  ",
                "queries": [
                    "  first   query ",
                    "",
                    "   ",
                    "first query",
                    long_query,
                    "third query",
                    "fourth query",
                    7,
                ],
            }
        ).decode()
        + "\n```"
    )

    plan = parse_query_reply(reply)

    assert plan == RerunPlan(
        queries=["first query", "q" * 200, "third query"],
        diagnosis="The vote date moved.",
    )


@pytest.mark.parametrize(
    "reply",
    [
        "no json at all",
        '{"diagnosis": "x", "queries": ',
        "[1, 2]",
        '{"queries": ["a"]}',
        '{"diagnosis": "x"}',
        '{"diagnosis": "x", "queries": []}',
        '{"diagnosis": "x", "queries": ["   "]}',
        '{"diagnosis": "   ", "queries": ["a"]}',
        '{"diagnosis": 4, "queries": ["a"]}',
        '{"diagnosis": "x", "queries": "a"}',
    ],
)
def test_parse_query_reply_rejects_unusable_replies(reply: str) -> None:
    assert parse_query_reply(reply) is None


def test_the_diagnosis_is_cut_to_300_characters() -> None:
    plan = parse_query_reply('{"diagnosis": "' + "d" * 400 + '", "queries": ["a"]}')

    assert plan is not None
    assert plan.diagnosis == "d" * 300


def test_the_query_prompt_carries_question_class_and_both_rationales() -> None:
    prompt = build_query_prompt(
        "Will X happen?",
        "Resolves YES if X.",
        "misread_resolution_criteria",
        RATIONALES,
    )

    assert "misread resolution criteria" in prompt
    assert "## Question\n\nWill X happen?" in prompt
    assert "## Resolution criteria\n\nResolves YES if X." in prompt
    assert "## Forecaster 1 reasoning\n\nRationale of the first forecaster." in prompt
    assert "## Forecaster 2 reasoning\n\nRationale of the second." in prompt
    assert "at most 200 characters" in prompt


@dataclass
class _Luna:
    """Stands in for `OpenRouterClient`; one scripted outcome per call."""

    reply: str | Exception | None
    calls: list[tuple[object, str]]

    async def complete(self, model: object, prompt: str, timeout: float) -> LLMResult:
        self.calls.append((model, prompt))
        if isinstance(self.reply, Exception):
            raise self.reply
        if self.reply is None:
            await asyncio.sleep(10)
        assert self.reply is not None
        return LLMResult(text=self.reply, cost_usd=0.004, tokens_in=1, tokens_out=1)


async def _write(luna: _Luna) -> RerunPlan:
    return await write_targeted_queries(
        luna,  # type: ignore[arg-type]
        "Will X happen?",
        "Resolves YES if X.",
        "stale_or_missing_information",
        RATIONALES,
        timeout=0.05,
    )


async def test_write_targeted_queries_returns_the_models_plan() -> None:
    luna = _Luna('{"diagnosis": "Vote moved.", "queries": ["vote date"]}', [])

    plan = await _write(luna)

    assert plan == RerunPlan(["vote date"], "Vote moved.")
    ((model, prompt),) = luna.calls
    assert model == REFEREE_MODEL
    assert prompt == build_query_prompt(
        "Will X happen?",
        "Resolves YES if X.",
        "stale_or_missing_information",
        RATIONALES,
    )


@pytest.mark.parametrize(
    "reply",
    [LLMError("down"), ValueError("not json"), None, "sorry, no JSON here"],
    ids=["llm-error", "non-json-body", "timeout", "unusable-reply"],
)
async def test_write_targeted_queries_falls_back_to_the_fixed_plan(
    reply: str | Exception | None,
) -> None:
    plan = await _write(_Luna(reply, []))

    assert plan == fixed_plan("stale_or_missing_information")
    assert plan.queries == []
    assert plan.diagnosis == (
        "The earlier research may have missed recent or relevant developments; "
        "weigh the newly gathered evidence carefully."
    )


def test_the_fixed_plan_for_misread_criteria() -> None:
    assert fixed_plan("misread_resolution_criteria") == RerunPlan(
        [],
        "The resolution criteria or fine print may have been misread; read them "
        "literally, edge cases included, before forecasting.",
    )


def test_the_rerun_note_holds_the_diagnosis_and_nothing_else() -> None:
    assert render_rerun_note("Vote moved.") == (
        "### Second round\n"
        "An earlier round of forecasts on this question disagreed, and the "
        "research above was refreshed to look into why. Likely cause: Vote moved."
    )
