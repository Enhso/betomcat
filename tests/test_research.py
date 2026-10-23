"""Tests for the IW client: happy path, family fail-open, degraded fallback."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import httpx
import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat.pipeline import FREE_NEWS_PROVIDERS
from betomcat.research import (
    ASKNEWS_SEARCH_URL,
    GOOGLE_NEWS_RSS_URL,
    ClaimView,
    Evidence,
    IWClient,
    ResearchResult,
    render_briefing_text,
    render_claims_text,
    render_history_text,
)

IW_URL = "http://iw.test"
QUESTION = "Will the ECB cut rates?"
ASKNEWS_REQUEST = re.compile(re.escape(ASKNEWS_SEARCH_URL) + r".*")
GOOGLE_REQUEST = re.compile(re.escape(GOOGLE_NEWS_RSS_URL) + r".*")
GOOGLE_FIXTURE = Path(__file__).parent / "fixtures" / "google_news_rss.xml"
DEGRADED_HEADER = "(degraded mode: direct news search fallback, IW unavailable)"

# The first 20 of the fixture's 60 items, newest first, source suffix stripped.
GOOGLE_FIXTURE_LINES = [
    "2026-10-08 | Reuters | "
    "Euro zone ministers tell France to pass 2027 budget to calm markets",
    "2026-10-08 | Morningstar | What Can the ECB Do About Rising French Bond Yields?",
    "2026-10-05 | The Mighty 790 KFGO | "
    "ECB\u2019s Lane: Drags on growth may limit need for ECB action",
    "2026-09-25 | Goldman Sachs | Why the ECB Is Unlikely to Keep Rates Higher Than 3%",
    "2026-09-25 | Financial Times | Yields up",
    "2026-09-24 | Investing.com | "
    "BofA sees ECB rate cuts resuming in second half of 2027",
    "2026-09-22 | Financial Times | "
    "A more hawkish ECB will respond to prolonged high energy prices",
    "2026-09-17 | BBC | "
    "What's happening to UK interest rates and what does it mean for mortgages?",
    "2026-09-17 | The House of Commons Library | "
    "Interest rates and monetary policy: Economic indicators",
    "2026-09-17 | Reuters | "
    "Bank of England shifts tone toward rate hikes as inflation set to top 4%",
    "2026-09-17 | CCN.com | "
    "Projected US Interest Rates in 5 Years: A Unanimous Fed Raises Interest Rates",
    "2026-09-16 | BBC | "
    "UK inflation: What is the rate and why are prices still rising?",
    "2026-09-14 | Africa News Agency | "
    "ECB: European inflation could remain higher than expected in 2027",
    "2026-09-14 | Reuters | "
    "Fed rate hike on Wednesday now likely, say economists, "
    "and at least one more to follow: Reuters Poll",
    "2026-09-11 | CCN.com | "
    "Projected ECB Interest Rate in 5 Years: "
    "Lagarde & Co. Raise Rates Again But Outlook Remains Uncertain",
    "2026-09-10 | Morningstar | "
    "ECB Hikes Rates and Raises Both Inflation and Growth Forecasts",
    "2026-09-10 | European Central Bank | "
    "ECB staff macroeconomic projections for the euro area, September 2026",
    "2026-09-10 | The Guardian | "
    "ECB raises interest rates to 2.5% and warns Iran war is fuelling inflation "
    "| European Central Bank",
    "2026-09-10 | chinadailyhk | "
    "ECB hikes rates again but remains cautious on future path",
    "2026-09-10 | Yahoo Finance | "
    "European Stocks Close Lower in Thursday Trading; "
    "European Central Bank Raises Interest Rates",
]
GOOGLE_SECTION = "Google News headlines:\n" + "\n".join(GOOGLE_FIXTURE_LINES)
ASKNEWS_ARTICLE_LINE = (
    "- **ECB signals caution** (2026-09-20T00:00:00Z): "
    "The ECB signalled caution on rate cuts. [https://example.com/a1]"
)
ASKNEWS_ARTICLES = {
    "as_dicts": [
        {
            "eng_title": "ECB signals caution",
            "article_url": "https://example.com/a1",
            "pub_date": "2026-09-20T00:00:00Z",
            "summary": "The ECB signalled caution on rate cuts.",
        }
    ]
}


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
        if request.url.host == "news.google.com":
            return httpx.Response(200, text="<rss><channel></channel></rss>")
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


def _google_feed() -> str:
    return GOOGLE_FIXTURE.read_text(encoding="utf-8")


async def _research_with_iw_down(
    client: IWClient, asknews_allowed: bool = True
) -> ResearchResult:
    """Ask for research the way the pipeline does (budget verdict as arguments)."""
    return await client.research(
        question=QUESTION,
        question_id="metaculus:1",
        context=None,
        family_id=None,
        timeout=30,
        providers=None if asknews_allowed else list(FREE_NEWS_PROVIDERS),
        asknews_allowed=asknews_allowed,
    )


def _outbox_entries(tmp_path: Path) -> list[dict[str, object]]:
    (outbox_file,) = (tmp_path / "outbox").glob("*.jsonl")
    return [orjson.loads(line) for line in outbox_file.read_bytes().splitlines()]


async def test_research_falls_back_on_5xx(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)
    httpx_mock.add_response(url=ASKNEWS_REQUEST, json=ASKNEWS_ARTICLES)
    httpx_mock.add_response(url=GOOGLE_REQUEST, text=_google_feed())
    client = _client(tmp_path)

    result = await _research_with_iw_down(client)

    assert result.degraded is True
    assert result.degraded_reason == "iw_unavailable"
    assert result.dossier_id is None
    assert result.asknews_calls == 1
    assert result.briefing_text == (
        f"{DEGRADED_HEADER}\n\n"
        f"AskNews latest news:\n{ASKNEWS_ARTICLE_LINE}\n\n"
        f"{GOOGLE_SECTION}"
    )
    assert _google_feed().count("<item>") == 60  # so the 20-line cap is exercised

    asknews_entry, google_entry = _outbox_entries(tmp_path)
    assert asknews_entry["provider"] == "asknews_news"
    assert asknews_entry["url"] == "https://example.com/a1"
    assert google_entry["provider"] == "google_news"
    assert google_entry["title"] == f"Google News headlines: {QUESTION}"
    assert google_entry["content"] == "\n".join(GOOGLE_FIXTURE_LINES)
    assert google_entry["published"] is None
    assert (
        google_entry["content_hash"]
        == hashlib.sha256("\n".join(GOOGLE_FIXTURE_LINES).encode("utf-8")).hexdigest()
    )


async def test_direct_fallback_sends_latest_news_and_google_rss_parameters(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)
    httpx_mock.add_response(url=ASKNEWS_REQUEST, json={"as_dicts": []})
    httpx_mock.add_response(url=GOOGLE_REQUEST, text=_google_feed())
    client = _client(tmp_path)

    await _research_with_iw_down(client)

    asknews_request = httpx_mock.get_request(url=ASKNEWS_REQUEST)
    assert asknews_request is not None
    assert asknews_request.method == "GET"
    assert asknews_request.url.copy_with(query=None) == httpx.URL(ASKNEWS_SEARCH_URL)
    assert dict(asknews_request.url.params) == {
        "query": QUESTION,
        "strategy": "latest news",
        "n_articles": "10",
        "return_type": "dicts",
        "method": "nl",
    }
    assert asknews_request.headers["Authorization"] == "Bearer ank_test"

    google_request = httpx_mock.get_request(url=GOOGLE_REQUEST)
    assert google_request is not None
    assert google_request.method == "GET"
    assert google_request.url.copy_with(query=None) == httpx.URL(GOOGLE_NEWS_RSS_URL)
    assert dict(google_request.url.params) == {
        "q": f"{QUESTION} when:30d",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    assert google_request.headers["User-Agent"] == "Mozilla/5.0"


async def test_direct_fallback_with_asknews_402_uses_google_news_alone(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)
    httpx_mock.add_response(
        url=ASKNEWS_REQUEST, status_code=402, json={"detail": "out of credits"}
    )
    httpx_mock.add_response(url=GOOGLE_REQUEST, text=_google_feed())
    client = _client(tmp_path)

    result = await _research_with_iw_down(client)

    assert result.degraded is True
    assert result.degraded_reason == "iw_unavailable"
    assert result.asknews_calls == 1
    assert result.briefing_text == f"{DEGRADED_HEADER}\n\n{GOOGLE_SECTION}"
    (google_entry,) = _outbox_entries(tmp_path)
    assert google_entry["provider"] == "google_news"
    assert google_entry["content"] == "\n".join(GOOGLE_FIXTURE_LINES)


@pytest.mark.parametrize(
    "google_response",
    [
        pytest.param({"status_code": 503}, id="google-5xx"),
        pytest.param({"text": "<rss><channel>"}, id="google-malformed-xml"),
        pytest.param(
            {"text": '<rss version="2.0"><channel><title>t</title></channel></rss>'},
            id="google-feed-without-items",
        ),
    ],
)
async def test_direct_fallback_with_no_source_answering_is_empty_research(
    httpx_mock: HTTPXMock, tmp_path: Path, google_response: dict[str, object]
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)
    httpx_mock.add_response(url=ASKNEWS_REQUEST, status_code=402)
    httpx_mock.add_response(url=GOOGLE_REQUEST, **google_response)
    client = _client(tmp_path)

    result = await _research_with_iw_down(client)

    assert result.degraded is True
    assert result.degraded_reason == "no_research"
    assert result.briefing_text == f"No research available for: {QUESTION}"
    assert result.asknews_calls == 1
    assert not (tmp_path / "outbox").exists()


async def test_direct_fallback_without_asknews_budget_makes_no_asknews_request(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(url=f"{IW_URL}/api/research", status_code=503)
    httpx_mock.add_response(url=GOOGLE_REQUEST, text=_google_feed())
    client = _client(tmp_path)

    result = await _research_with_iw_down(client, asknews_allowed=False)

    assert httpx_mock.get_requests(url=ASKNEWS_REQUEST) == []
    assert result.asknews_calls == 0
    assert result.degraded_reason == "iw_unavailable"
    assert result.briefing_text == f"{DEGRADED_HEADER}\n\n{GOOGLE_SECTION}"
    iw_request = httpx_mock.get_request(url=f"{IW_URL}/api/research")
    assert iw_request is not None
    assert orjson.loads(iw_request.content)["providers"] == [
        "google_news",
        "bing_news",
        "wikipedia",
    ]
    (google_entry,) = _outbox_entries(tmp_path)
    assert google_entry["provider"] == "google_news"


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
