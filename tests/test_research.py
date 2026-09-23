"""Tests for the IW client: happy path, family fail-open, degraded fallback."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import orjson
from pytest_httpx import HTTPXMock

from betomcat.research import (
    ASKNEWS_SEARCH_URL,
    ClaimView,
    Evidence,
    IWClient,
    render_briefing_text,
    render_claims_text,
    render_history_text,
)

IW_URL = "http://iw.test"


def _client(tmp_path: Path, asknews_key: str | None = "ank_test") -> IWClient:
    return IWClient(IW_URL, asknews_key, tmp_path)


async def test_classify_family_matched(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        url=f"{IW_URL}/api/families/classify",
        json={
            "family_id": "fam:ecb-rate-decisions",
            "label": "ECB rate decisions",
            "decision": "matched",
            "probability": 0.83,
            "method": "jev",
            "gate_log": [],
        },
    )
    client = _client(tmp_path)

    result = await client.classify_family("Will the ECB cut rates?")

    assert result.decision == "matched"
    assert result.family_id == "fam:ecb-rate-decisions"


async def test_classify_family_fails_open_on_error(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/families/classify", status_code=500)
    client = _client(tmp_path)

    result = await client.classify_family("Will the ECB cut rates?")

    assert result.decision == "none"
    assert result.family_id is None
    assert result.method == "failed"


async def test_classify_family_fails_open_on_connect_error(tmp_path: Path) -> None:
    async def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(raise_connect_error)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = IWClient(IW_URL, "ank_test", tmp_path, client=http_client)
        result = await client.classify_family("Will the ECB cut rates?")

    assert result.decision == "none"


async def test_research_happy_path(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        url=f"{IW_URL}/api/research",
        json={
            "dossier_id": "dos:abc",
            "as_of": "2026-09-22T19:30:12Z",
            "counts": {},
            "briefing": {"situation_summary": "Calm markets."},
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "Inflation is falling.",
                    "kind": "fact",
                    "support": 0.9,
                    "support_method": "jev",
                    "dossier_id": "dos:abc",
                    "evidence": [],
                }
            ],
            "family_claims": [],
            "history": [],
            "gate_log": [],
            "dropped_sources": [],
        },
    )
    client = _client(tmp_path)

    result = await client.research(
        question="Will the ECB cut rates?",
        question_id="metaculus:1",
        context=None,
        family_id="fam:ecb-rate-decisions",
        timeout=30,
    )

    assert result.degraded is False
    assert result.dossier_id == "dos:abc"
    assert len(result.claims) == 1
    assert "Calm markets." in result.briefing_text


async def test_research_falls_back_to_asknews_on_connect_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/research":
            raise httpx.ConnectError("connection refused", request=request)
        assert request.url.path == "/v1/news/search"
        return httpx.Response(
            200,
            json={
                "as_dicts": [
                    {
                        "eng_title": "ECB signals caution",
                        "article_url": "https://example.com/a1",
                        "pub_date": "2026-09-20T00:00:00Z",
                        "summary": "The ECB signalled caution on rate cuts.",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = IWClient(IW_URL, "ank_test", tmp_path, client=http_client)
        result = await client.research(
            question="Will the ECB cut rates?",
            question_id="metaculus:1",
            context=None,
            family_id=None,
            timeout=30,
        )

    assert result.degraded is True
    assert result.degraded_reason == "iw_unavailable"
    assert "ECB signals caution" in result.briefing_text

    outbox_files = list((tmp_path / "outbox").glob("*.jsonl"))
    assert len(outbox_files) == 1
    lines = outbox_files[0].read_text().strip().splitlines()
    assert len(lines) == 1
    entry = orjson.loads(lines[0])
    assert entry["url"] == "https://example.com/a1"
    assert entry["provider"] == "asknews_news"
    assert "content_hash" in entry


async def test_research_falls_back_on_5xx(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)
    httpx_mock.add_response(
        url=re.compile(re.escape(ASKNEWS_SEARCH_URL) + r".*"),
        json={"as_dicts": []},
    )
    client = _client(tmp_path)

    result = await client.research(
        question="Will the ECB cut rates?",
        question_id="metaculus:1",
        context=None,
        family_id=None,
        timeout=30,
    )

    assert result.degraded is True
    assert result.degraded_reason == "iw_unavailable"
    assert "no articles found" in result.briefing_text


async def test_research_no_asknews_key_proceeds_question_only(tmp_path: Path) -> None:
    async def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(raise_connect_error)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = IWClient(IW_URL, None, tmp_path, client=http_client)
        result = await client.research(
            question="Will the ECB cut rates?",
            question_id="metaculus:1",
            context=None,
            family_id=None,
            timeout=30,
        )

    assert result.degraded is True
    assert result.degraded_reason == "no_research"
    assert "No research available" in result.briefing_text


async def test_research_asknews_also_fails_flags_no_research(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/research":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(500, text="asknews down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = IWClient(IW_URL, "ank_test", tmp_path, client=http_client)
        result = await client.research(
            question="Will the ECB cut rates?",
            question_id=None,
            context=None,
            family_id=None,
            timeout=30,
        )

    assert result.degraded is True
    assert result.degraded_reason == "no_research"


def test_render_briefing_text_renders_sections() -> None:
    text = render_briefing_text(
        {
            "situation_summary": "Calm markets.",
            "competing_hypotheses": ["Hypothesis A", "Hypothesis B"],
        }
    )

    assert "### Situation Summary" in text
    assert "Calm markets." in text
    assert "### Competing Hypotheses" in text
    assert "Hypothesis A" in text


def test_render_briefing_text_empty() -> None:
    assert render_briefing_text(None) == "(no briefing available)"
    assert render_briefing_text({}) == "(no briefing available)"


def test_render_claims_text_includes_evidence() -> None:
    claims = [
        ClaimView(
            claim_id="c1",
            text="Inflation is falling.",
            kind="fact",
            support=0.9,
            support_method="jev",
            dossier_id="dos:1",
            evidence=[
                Evidence(
                    source_id="src:1",
                    title="CPI report",
                    url="https://example.com/cpi",
                    provider="asknews_news",
                    published="2026-09-01T00:00:00Z",
                    fetched_at="2026-09-20T00:00:00Z",
                    content_hash="abc123def456",
                )
            ],
        )
    ]

    text = render_claims_text(claims)

    assert "support=0.90" in text
    assert "CPI report" in text
    assert "abc123def456"[:12] in text


def test_render_claims_text_empty() -> None:
    assert render_claims_text([]) == "(none)"


def test_render_history_text_empty() -> None:
    assert render_history_text([]) == "(none)"
