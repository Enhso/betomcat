"""Async wrapper over `forecasting_tools`' (sync, requests-based) MetaculusClient.

Every blocking call is pushed to a thread via `asyncio.to_thread`. In
`DRY_RUN` mode, submissions and comments are logged instead of sent.
`ConditionalQuestion`s are filtered out of tournament listings with a
logged reason -- this chunk does not forecast on them.
"""

from __future__ import annotations

import asyncio
import logging

from forecasting_tools.data_models.questions import (
    ConditionalQuestion,
    MetaculusQuestion,
)
from forecasting_tools.helpers.metaculus_client import MetaculusClient

logger = logging.getLogger(__name__)


def _question_id(question: MetaculusQuestion) -> int:
    if question.id_of_question is None:
        raise ValueError("question has no id_of_question; cannot submit a prediction")
    return int(question.id_of_question)


class MetaculusWrapper:
    """Async-friendly facade over `MetaculusClient` for the pipeline to consume."""

    def __init__(
        self,
        token: str | None,
        dry_run: bool,
        client: MetaculusClient | None = None,
    ) -> None:
        self._dry_run = dry_run
        self._client = client or MetaculusClient(token=token)

    async def list_open_questions(
        self, tournament: int | str
    ) -> list[MetaculusQuestion]:
        """Open questions for `tournament`, with `ConditionalQuestion`s dropped."""
        questions = await asyncio.to_thread(
            self._client.get_all_open_questions_from_tournament, tournament
        )
        kept: list[MetaculusQuestion] = []
        for question in questions:
            if isinstance(question, ConditionalQuestion):
                logger.info(
                    "skipping conditional question %s: unsupported question type",
                    question.id_of_post,
                )
                continue
            kept.append(question)
        return kept

    async def get_question_by_url(self, url: str) -> MetaculusQuestion:
        return await asyncio.to_thread(self._client.get_question_by_url, url)

    @staticmethod
    def already_forecasted(question: MetaculusQuestion) -> bool:
        return bool(question.already_forecasted)

    async def post_binary(
        self, question: MetaculusQuestion, probability: float
    ) -> None:
        question_id = _question_id(question)
        if self._dry_run:
            logger.info(
                "[dry-run] would post binary %.4f on question %s",
                probability,
                question_id,
            )
            return
        await asyncio.to_thread(
            self._client.post_binary_question_prediction, question_id, probability
        )

    async def post_multiple_choice(
        self, question: MetaculusQuestion, options: dict[str, float]
    ) -> None:
        question_id = _question_id(question)
        if self._dry_run:
            logger.info(
                "[dry-run] would post multiple-choice %s on question %s",
                options,
                question_id,
            )
            return
        await asyncio.to_thread(
            self._client.post_multiple_choice_question_prediction, question_id, options
        )

    async def post_numeric(
        self, question: MetaculusQuestion, cdf_values: list[float]
    ) -> None:
        question_id = _question_id(question)
        if self._dry_run:
            logger.info(
                "[dry-run] would post numeric cdf[%d pts] on question %s",
                len(cdf_values),
                question_id,
            )
            return
        await asyncio.to_thread(
            self._client.post_numeric_question_prediction, question_id, cdf_values
        )

    async def post_comment(self, question: MetaculusQuestion, text: str) -> None:
        if question.id_of_post is None:
            raise ValueError("question has no id_of_post; cannot post a comment")
        if self._dry_run:
            logger.info(
                "[dry-run] would post comment (%d chars) on post %s:\n%s",
                len(text),
                question.id_of_post,
                text,
            )
            return
        await asyncio.to_thread(
            self._client.post_question_comment, question.id_of_post, text
        )
