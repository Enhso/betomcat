"""Tests for mail.py: the Resend request and its failure modes."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import httpx
import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat.config import Settings, load_settings
from betomcat.mail import RESEND_URL, mail_credentials, send_email


def _settings(**overrides: object) -> Settings:
    base = Settings(
        metaculus_token=None,
        openrouter_api_key=None,
        openrouter_free_api_key=None,
        asknews_api_key=None,
        iw_url="http://127.0.0.1:8080",
        data_dir=Path("data"),
        soft_threshold_min=30,
        hard_threshold_min=5,
        poll_seconds=300,
        dry_run=False,
        resend_api_key="re_test_key",
        summary_to="owner@example.org",
    )
    return dataclasses.replace(base, **overrides)


async def test_send_email_posts_the_resend_body_with_a_bearer_key(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=RESEND_URL, method="POST", json={"id": "abc"})

    async with httpx.AsyncClient() as client:
        await send_email(client, _settings(), "Subject line", "<p>hi</p>", "hi")

    (request,) = httpx_mock.get_requests()
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == "Bearer re_test_key"
    assert orjson.loads(request.content) == {
        "from": "betomcat <onboarding@resend.dev>",
        "to": "owner@example.org",
        "subject": "Subject line",
        "html": "<p>hi</p>",
        "text": "hi",
    }


async def test_send_email_raises_on_a_non_2xx_without_leaking_the_key(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=RESEND_URL, status_code=403, json={"message": "no"})

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await send_email(client, _settings(), "s", "<p>h</p>", "h")

    assert "re_test_key" not in str(caught.value)
    assert "owner@example.org" not in str(caught.value)


@pytest.mark.parametrize(
    "missing", [{"resend_api_key": None}, {"summary_to": None}, {"summary_to": ""}]
)
async def test_send_email_exits_without_the_key_or_the_recipient(
    missing: dict[str, object], httpx_mock: HTTPXMock
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(SystemExit, match="RESEND_API_KEY and SUMMARY_TO"):
            await send_email(client, _settings(**missing), "s", "<p>h</p>", "h")

    assert httpx_mock.get_requests() == []


def test_mail_credentials_returns_the_key_and_the_recipient() -> None:
    assert mail_credentials(_settings()) == ("re_test_key", "owner@example.org")


def test_load_settings_reads_the_mail_and_control_keys_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_env")
    monkeypatch.setenv("SUMMARY_TO", "me@example.org")
    monkeypatch.setenv("V2_METACULUS_TOKEN", "v2-token")

    settings = load_settings(env_path="/nonexistent/.env")

    assert settings.resend_api_key == "re_env"
    assert settings.summary_to == "me@example.org"
    assert settings.v2_metaculus_token == "v2-token"


def test_load_settings_leaves_the_mail_and_control_keys_unset_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("RESEND_API_KEY", "SUMMARY_TO", "V2_METACULUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(env_path="/nonexistent/.env")

    assert settings.resend_api_key is None
    assert settings.summary_to is None
    assert settings.v2_metaculus_token is None
