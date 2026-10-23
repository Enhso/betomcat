"""Read-only fetchers behind `betomcat summary` and `betomcat watch`.

Everything here is a GET. Metaculus needs a browser User-Agent and at least
one second between requests; each bot token filters `forecaster_id` only on
its own id, so every Metaculus call takes the token to use. The repo and its
Actions logs are public: failures log the exception class only, never a URL
with credentials or a response body.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tarfile
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from cryptography.fernet import InvalidToken

from betomcat.budget import BROWSER_USER_AGENT
from betomcat.state import GITHUB_API

logger = logging.getLogger(__name__)

METACULUS_API = "https://www.metaculus.com/api"
METACULUS_PAUSE_SECONDS = 1.0
POSTS_PAGE_SIZE = 100
MAX_POSTS_PAGES = 10
REQUEST_TIMEOUT = 30.0

BETOMCAT_REPO = "Enhso/betomcat"
VEZOCONTROL_REPO = "Enhso/vezocontrol"
HOST_WORKFLOW = "host.yml"
CONTROL_WORKFLOW = "run_bot_on_tournament.yaml"
FE_FALL_PROJECT_ID = 33121


@dataclass(frozen=True)
class Bot:
    """A forecasting account: leaderboard username and Metaculus user id."""

    username: str
    user_id: int


VEZO3 = Bot("vezo3", 308852)
VEZOCONTROL = Bot("vezocontrol", 308853)

SOURCE_ERRORS = (
    httpx.HTTPError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    sqlite3.Error,
    tarfile.TarError,
    InvalidToken,
)

T = TypeVar("T")


async def attempt(label: str, call: Awaitable[T]) -> T | None:
    """Await `call`; on a source failure log the class name and return None."""
    try:
        return await call
    except SOURCE_ERRORS as exc:
        logger.warning("%s unavailable (%s)", label, type(exc).__name__)
        return None


@dataclass(frozen=True)
class Standing:
    """A bot's line on one leaderboard."""

    rank: int | None
    score: float | None
    coverage: float | None
    finalized: bool

    def cells(self) -> tuple[str, str, str]:
        """Rank, score and coverage as display strings."""
        return (
            "-" if self.rank is None else str(self.rank),
            "-" if self.score is None else f"{self.score:.2f}",
            "-" if self.coverage is None else f"{self.coverage:.2f}",
        )

    def describe(self) -> str:
        """One line: `rank 49, score 272.00, coverage 0.98`."""
        rank, score, coverage = self.cells()
        return f"rank {rank}, score {score}, coverage {coverage}"


def find_standing(leaderboards: list[dict[str, Any]], username: str) -> Standing | None:
    """The first leaderboard entry whose `user.username` is `username`.

    Field names (`entries`, `finalized`, `user.username`, `rank`, `score`,
    `coverage`) are read here and nowhere else.
    """
    for leaderboard in leaderboards:
        for entry in leaderboard.get("entries", []):
            if (entry.get("user") or {}).get("username") == username:
                return Standing(
                    rank=entry.get("rank"),
                    score=entry.get("score"),
                    coverage=entry.get("coverage"),
                    finalized=bool(leaderboard.get("finalized")),
                )
    return None


def round_is_finalized(leaderboards: list[dict[str, Any]]) -> bool:
    """Whether vezo3's leaderboard for the round is finalized."""
    standing = find_standing(leaderboards, VEZO3.username)
    return standing is not None and standing.finalized


def last_forecast_time(post: dict[str, Any]) -> datetime | None:
    """When the token's user last forecast on `post`, if the list says so."""
    if "question" in post:
        questions = [post["question"]]
    else:
        questions = (post.get("group_of_questions") or {}).get("questions", [])
    stamps = []
    for question in questions:
        latest = ((question or {}).get("my_forecasts") or {}).get("latest") or {}
        start = latest.get("start_time")
        if isinstance(start, int | float):
            stamps.append(datetime.fromtimestamp(start, UTC))
    return max(stamps, default=None)


class MetaculusReader:
    """GETs against the Metaculus API, one second apart."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._last_request: float | None = None

    async def _get(
        self, path: str, token: str | None, params: dict[str, Any] | None = None
    ) -> Any:
        if self._last_request is not None:
            wait = METACULUS_PAUSE_SECONDS - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
        headers = {"User-Agent": BROWSER_USER_AGENT}
        if token:
            headers["Authorization"] = f"Token {token}"
        try:
            response = await self._client.get(
                f"{METACULUS_API}{path}",
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response.json()

    async def leaderboards(
        self, project_id: int, token: str | None
    ) -> list[dict[str, Any]]:
        """`GET /leaderboards/project/<id>/`: a list of leaderboards."""
        data = await self._get(f"/leaderboards/project/{project_id}/", token)
        if not isinstance(data, list):
            raise TypeError("leaderboards payload is not a list")
        return data

    async def minibench_id(self, token: str | None) -> int:
        """The active MiniBench round's project id."""
        data = await self._get("/projects/tournaments/minibench/", token)
        return int(data["id"])

    async def posts(self, token: str | None, **filters: Any) -> list[dict[str, Any]]:
        """Every post matching `filters` on `GET /posts/`, paged by 100."""
        posts: list[dict[str, Any]] = []
        for page in range(MAX_POSTS_PAGES):
            data = await self._get(
                "/posts/",
                token,
                {**filters, "limit": POSTS_PAGE_SIZE, "offset": page * POSTS_PAGE_SIZE},
            )
            results = data["results"]
            posts.extend(results)
            if len(results) < POSTS_PAGE_SIZE:
                break
        return posts


class GitHubReader:
    """GETs against the GitHub REST API; anonymous when there is no token."""

    def __init__(self, client: httpx.AsyncClient, token: str | None) -> None:
        self._client = client
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(
            f"{GITHUB_API}{path}",
            headers=self._headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    async def workflow_runs(self, repo: str, workflow: str) -> list[dict[str, Any]]:
        """The workflow's 20 newest runs."""
        data = await self._get(
            f"/repos/{repo}/actions/workflows/{workflow}/runs", {"per_page": 20}
        )
        return list(data["workflow_runs"])

    async def failed_steps(self, repo: str, run_id: int) -> tuple[str, ...]:
        """Names of the steps that failed in a run, as `job: step`."""
        data = await self._get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
        names: list[str] = []
        for job in data["jobs"]:
            if job.get("conclusion") != "failure":
                continue
            steps = [
                step["name"]
                for step in job.get("steps", [])
                if step.get("conclusion") == "failure"
            ]
            names.extend([f"{job['name']}: {step}" for step in steps] or [job["name"]])
        return tuple(names)

    async def open_pulls(self, repo: str) -> list[dict[str, Any]]:
        """Open pull requests."""
        data = await self._get(f"/repos/{repo}/pulls", {"state": "open"})
        return list(data)

    async def commits_touching(
        self, repo: str, path: str, since: datetime
    ) -> list[dict[str, Any]]:
        """Commits that touched `path` since `since`."""
        data = await self._get(
            f"/repos/{repo}/commits",
            {"path": path, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        return list(data)
