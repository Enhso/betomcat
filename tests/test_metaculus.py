"""Tests for the async MetaculusClient wrapper: dry-run and conditional skip."""

from __future__ import annotations

from unittest.mock import MagicMock

from forecasting_tools.data_models.questions import BinaryQuestion, ConditionalQuestion

from betomcat.metaculus import MetaculusWrapper


def _binary_question(question_id: int = 1) -> BinaryQuestion:
    return BinaryQuestion(
        question_text="Will X?",
        id_of_post=question_id,
        id_of_question=question_id,
    )


async def test_dry_run_post_binary_does_not_call_client() -> None:
    fake_client = MagicMock()
    wrapper = MetaculusWrapper(token=None, dry_run=True, client=fake_client)

    await wrapper.post_binary(_binary_question(), 0.62)

    fake_client.post_binary_question_prediction.assert_not_called()


async def test_live_post_binary_calls_client() -> None:
    fake_client = MagicMock()
    wrapper = MetaculusWrapper(token=None, dry_run=False, client=fake_client)

    await wrapper.post_binary(_binary_question(42), 0.62)

    fake_client.post_binary_question_prediction.assert_called_once_with(42, 0.62)


async def test_dry_run_post_comment_does_not_call_client() -> None:
    fake_client = MagicMock()
    wrapper = MetaculusWrapper(token=None, dry_run=True, client=fake_client)

    await wrapper.post_comment(_binary_question(), "some comment")

    fake_client.post_question_comment.assert_not_called()


async def test_list_open_questions_skips_conditional() -> None:
    conditional = ConditionalQuestion(
        question_text="conditional",
        id_of_post=1,
        id_of_question=1,
        parent=_binary_question(2),
        child=_binary_question(3),
        question_yes=_binary_question(4),
        question_no=_binary_question(5),
    )
    binary = _binary_question(6)
    fake_client = MagicMock()
    fake_client.get_all_open_questions_from_tournament.return_value = [
        conditional,
        binary,
    ]
    wrapper = MetaculusWrapper(token=None, dry_run=True, client=fake_client)

    questions = await wrapper.list_open_questions("bot-testing-area")

    assert questions == [binary]


def test_already_forecasted() -> None:
    question = _binary_question()
    question.already_forecasted = True
    assert MetaculusWrapper.already_forecasted(question) is True

    question.already_forecasted = None
    assert MetaculusWrapper.already_forecasted(question) is False
