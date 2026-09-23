"""Intelligence Workbench (IW) HTTP client, plus the degraded-mode fallback.

Talks to IW over localhost HTTP (contracts.md s C). If IW is unreachable
(connection refused) or returns 5xx on `/api/research`, falls back to a
direct AskNews call and spools the fetched documents to an outbox for later
replay into IW (`betomcat replay-outbox`), so the as-of discipline holds
even in degraded mode (BUILD_LOG decision 3). If IW is merely slow, the
caller's own timeout governs -- there is no thin-briefing shortcut here.

`/api/families/classify` fails open to "no family" on any error, with a
60s timeout.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import orjson
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ASKNEWS_SEARCH_URL = "https://api.asknews.app/v1/news/search"
FAMILY_CLASSIFY_TIMEOUT = 60.0
DIRECT_ASKNEWS_TIMEOUT = 30.0


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Evidence(BaseModel):
    source_id: str
    title: str
    url: str
    provider: str
    published: str | None = None
    fetched_at: str
    content_hash: str
    stance: str | None = None
    excerpt: str = ""


class ClaimView(BaseModel):
    claim_id: str
    text: str
    kind: str
    support: float | None = None
    support_method: str = "none"
    dossier_id: str
    evidence: list[Evidence] = Field(default_factory=list)


class HistoryItem(BaseModel):
    id: str
    kind: str
    title: str
    url: str
    question_type: str
    forecast: Any = None
    resolution: str | None = None
    resolved_at: str | None = None
    family_id: str | None = None
    note: str = ""


class FamilyClassification(BaseModel):
    family_id: str | None = None
    label: str | None = None
    decision: str = "none"
    probability: float | None = None
    method: str = "failed"
    gate_log: list[dict[str, Any]] = Field(default_factory=list)


class ResearchResult(BaseModel):
    dossier_id: str | None = None
    as_of: str
    briefing_text: str
    claims: list[ClaimView] = Field(default_factory=list)
    family_claims: list[ClaimView] = Field(default_factory=list)
    history: list[HistoryItem] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    gate_log: list[dict[str, Any]] = Field(default_factory=list)


def render_briefing_text(briefing: dict[str, Any] | None) -> str:
    """Render the Briefing JSON's sections as readable text for a prompt."""
    if not briefing:
        return "(no briefing available)"
    sections = [
        f"### {str(key).replace('_', ' ').strip().title()}\n{_render_value(value, 0)}"
        for key, value in briefing.items()
    ]
    return "\n\n".join(sections)


def _render_value(value: Any, indent: int) -> str:
    prefix = "  " * indent
    if isinstance(value, dict):
        if not value:
            return f"{prefix}(none)"
        lines = []
        for key, sub in value.items():
            label = str(key).replace("_", " ").strip()
            rendered = _render_value(sub, indent + 1)
            sep = "\n" if "\n" in rendered else " "
            lines.append(
                f"{prefix}- {label}:{sep}{rendered.strip() if sep == ' ' else rendered}"
            )
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{prefix}(none)"
        lines = []
        for item in value:
            rendered = _render_value(item, indent + 1)
            if "\n" in rendered:
                lines.append(f"{prefix}-\n{rendered}")
            else:
                lines.append(f"{prefix}- {rendered.strip()}")
        return "\n".join(lines)
    return f"{prefix}{value}"


def render_claims_text(claims: list[ClaimView]) -> str:
    """Render claims (with provenance) as readable text for a prompt."""
    if not claims:
        return "(none)"
    blocks = []
    for claim in claims:
        support = "n/a" if claim.support is None else f"{claim.support:.2f}"
        lines = [f"- [support={support}, kind={claim.kind}] {claim.text}"]
        for ev in claim.evidence:
            published = ev.published or "unknown"
            lines.append(
                f"    - {ev.title} ({ev.provider}, published {published}, "
                f"fetched {ev.fetched_at}) {ev.url} [hash {ev.content_hash[:12]}]"
            )
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def render_history_text(history: list[HistoryItem]) -> str:
    """Render personal/bot forecasting history as readable text for a prompt."""
    if not history:
        return "(none)"
    lines = []
    for item in history:
        resolution = item.resolution or "unresolved"
        lines.append(
            f"- [{item.kind}] {item.title} -- forecast: {item.forecast!r}, "
            f"resolution: {resolution} ({item.url})"
        )
    return "\n".join(lines)


class IWClient:
    """Async client for the Intelligence Workbench HTTP API."""

    def __init__(
        self,
        base_url: str,
        asknews_api_key: str | None,
        data_dir: Path | str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._asknews_api_key = asknews_api_key
        self._data_dir = Path(data_dir)
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ingest_documents(self, documents: list[dict[str, Any]]) -> int:
        """`POST /api/documents`: ingest raw documents without extraction.

        Used by `betomcat replay-outbox` to replay degraded-mode fetches
        (spooled by `_direct_asknews`) back into IW once it is reachable
        again. Only the fields IW's contract documents are sent; local
        bookkeeping fields (e.g. `content_hash`) are dropped -- IW computes
        its own ids and hashes.
        """
        payload = {
            "documents": [
                {
                    "url": doc["url"],
                    "title": doc["title"],
                    "provider": doc["provider"],
                    "published": doc.get("published"),
                    "fetched_at": doc["fetched_at"],
                    "content": doc["content"],
                }
                for doc in documents
            ]
        }
        response = await self._client.post(
            f"{self._base_url}/api/documents", json=payload, timeout=60
        )
        response.raise_for_status()
        return int(response.json().get("ingested", 0))

    async def classify_family(
        self,
        question: str,
        question_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> FamilyClassification:
        """`POST /api/families/classify`. Fails open to "no family"."""
        try:
            response = await self._client.post(
                f"{self._base_url}/api/families/classify",
                json={
                    "question": question,
                    "question_id": question_id,
                    "context": context,
                },
                timeout=FAMILY_CLASSIFY_TIMEOUT,
            )
            response.raise_for_status()
            return FamilyClassification.model_validate(response.json())
        except Exception as exc:
            logger.warning("family classify failed open: %s", exc)
            return FamilyClassification()

    async def research(
        self,
        *,
        question: str,
        question_id: str | None,
        context: dict[str, Any] | None,
        family_id: str | None,
        timeout: float,
        providers: list[str] | None = None,
        news_since: str | None = None,
        max_news: int = 12,
        max_wiki: int = 3,
    ) -> ResearchResult:
        """`POST /api/research`, falling back to direct AskNews when IW is down.

        Args:
            question: Question title.
            question_id: Metaculus question id, e.g. `"metaculus:41234"`.
            context: `{resolution_criteria, fine_print, background}`.
            family_id: The question's family, if already classified.
            timeout: Request timeout in seconds (bounded by the caller's hard
                deadline; if IW is merely slow, this is where that time goes).
            providers: Override research providers.
            news_since: Explicit gap-fill floor; normally left `None` so IW
                resolves it server-side from the family's `last_seen`.
            max_news: Max news documents to fetch.
            max_wiki: Max wiki documents to fetch.

        Returns:
            The dossier's research result, or a degraded direct-AskNews
            result if IW was unreachable or errored server-side.
        """
        body: dict[str, Any] = {
            "question": question,
            "question_id": question_id,
            "context": context,
            "family_id": family_id,
            "news_since": news_since,
            "max_news": max_news,
            "max_wiki": max_wiki,
        }
        if providers is not None:
            body["providers"] = providers

        try:
            response = await self._client.post(
                f"{self._base_url}/api/research", json=body, timeout=timeout
            )
        except httpx.ConnectError as exc:
            logger.warning("IW unreachable, falling back to direct AskNews: %s", exc)
            return await self._direct_asknews(question)

        if response.status_code >= 500:
            logger.warning(
                "IW research returned %s, falling back to direct AskNews",
                response.status_code,
            )
            return await self._direct_asknews(question)
        response.raise_for_status()
        return self._parse_research_response(response.json())

    def _parse_research_response(self, data: dict[str, Any]) -> ResearchResult:
        return ResearchResult(
            dossier_id=data.get("dossier_id"),
            as_of=data.get("as_of", _now_iso()),
            briefing_text=render_briefing_text(data.get("briefing")),
            claims=[ClaimView.model_validate(c) for c in data.get("claims", [])],
            family_claims=[
                ClaimView.model_validate(c) for c in data.get("family_claims", [])
            ],
            history=[HistoryItem.model_validate(h) for h in data.get("history", [])],
            degraded=False,
            gate_log=data.get("gate_log", []),
        )

    async def _direct_asknews(self, question: str) -> ResearchResult:
        """Degraded-mode research: direct AskNews, with outbox spooling."""
        as_of = _now_iso()
        if not self._asknews_api_key:
            logger.warning("no AskNews key configured, proceeding question-only")
            return ResearchResult(
                as_of=as_of,
                briefing_text=f"No research available for: {question}",
                degraded=True,
                degraded_reason="no_research",
            )

        try:
            response = await self._client.get(
                ASKNEWS_SEARCH_URL,
                headers={"Authorization": f"Bearer {self._asknews_api_key}"},
                params={
                    "query": question,
                    "n_articles": 10,
                    "return_type": "dicts",
                    "method": "nl",
                    "hours_back": 720,
                },
                timeout=DIRECT_ASKNEWS_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("direct AskNews fallback also failed: %s", exc)
            return ResearchResult(
                as_of=as_of,
                briefing_text=f"No research available for: {question}",
                degraded=True,
                degraded_reason="no_research",
            )

        articles = payload.get("as_dicts") or []
        fetched_at = _now_iso()
        rendered_articles = []
        outbox_lines: list[bytes] = []
        for article in articles:
            title = article.get("eng_title") or article.get("title") or "(untitled)"
            url = article.get("article_url") or article.get("url") or ""
            published = article.get("pub_date") or article.get("published")
            content = (
                article.get("summary")
                or article.get("article_summary")
                or article.get("content")
                or ""
            )
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            published_display = published or "unknown date"
            rendered_articles.append(
                f"- **{title}** ({published_display}): {content[:500]} [{url}]"
            )
            outbox_lines.append(
                orjson.dumps(
                    {
                        "url": url,
                        "title": title,
                        "provider": "asknews_news",
                        "published": published,
                        "fetched_at": fetched_at,
                        "content": content,
                        "content_hash": content_hash,
                    }
                )
            )

        self._write_outbox(outbox_lines)

        briefing_text = (
            "(degraded mode: direct AskNews fallback, IW unavailable)\n\n"
            + (
                "\n".join(rendered_articles)
                if rendered_articles
                else "(no articles found)"
            )
        )
        return ResearchResult(
            as_of=as_of,
            briefing_text=briefing_text,
            degraded=True,
            degraded_reason="iw_unavailable",
        )

    def _write_outbox(self, lines: list[bytes]) -> None:
        if not lines:
            return
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        outbox_dir = self._data_dir / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        path = outbox_dir / f"{date_str}.jsonl"
        with open(path, "ab") as f:
            for line in lines:
                f.write(line + b"\n")
