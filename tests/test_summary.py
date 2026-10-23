"""Tests for summary.py: the sections and numbers of the weekly summary.

Every HTTP call is mocked through pytest_httpx and the ledger is seeded; the
tests call `run_summary` as `betomcat summary` does.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from cryptography.fernet import Fernet
from pytest_httpx import HTTPXMock

from betomcat import sources
from betomcat.cli import main
from betomcat.config import Settings
from betomcat.ledger import Ledger
from betomcat.pool import write_weights
from betomcat.state import build_snapshot_archive, encrypt_file
from betomcat.summary import run_summary

NOW = datetime(2026, 10, 26, 7, 0, 5, tzinfo=UTC)
KEY = Fernet.generate_key()
REPO = "Enhso/betomcat"
GITHUB = "https://api.github.com"
METACULUS = "https://www.metaculus.com/api"
RELEASES = f"{GITHUB}/repos/{REPO}/releases?per_page=100"
RESEND = "https://api.resend.com/emails"
MODEL_A, MODEL_B, MODEL_C = "openai/model-a", "openai/model-b", "openai/model-c"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "METACULUS_PAUSE_SECONDS", 0.0)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token-secret")
    monkeypatch.setenv("STATE_KEY", KEY.decode())


def _settings() -> Settings:
    return Settings(
        metaculus_token="v1-token",
        openrouter_api_key="or-key-secret",
        openrouter_free_api_key=None,
        asknews_api_key=None,
        iw_url="http://127.0.0.1:8080",
        data_dir=Path("data"),
        soft_threshold_min=30,
        hard_threshold_min=5,
        poll_seconds=300,
        dry_run=False,
        v2_metaculus_token="v2-token",
        resend_api_key="re_secret_key",
        summary_to="owner@example.org",
    )


def _seed_snapshot(tmp_path: Path) -> bytes:
    """An encrypted snapshot whose ledger holds one week of known activity."""
    data_dir = tmp_path / "data"
    with Ledger(data_dir / "ledger.sqlite"):
        pass
    conn = sqlite3.connect(data_dir / "ledger.sqlite")
    for n in range(1, 6):
        conn.execute(
            "INSERT INTO questions VALUES (?, 't', 'binary', NULL, NULL, ?)",
            (f"q{n}", "2026-10-01T00:00:00Z"),
        )
    runs = [
        (1, "q1", "ok", 0, "2026-10-25T10:00:00Z"),
        (2, "q2", "ok", 1, "2026-10-24T10:00:00Z"),
        (3, "q3", "failed", 0, "2026-10-23T10:00:00Z"),
        (4, "q4", None, 0, "2026-10-26T01:00:00Z"),
        (5, "q5", "ok", 0, "2026-10-15T10:00:00Z"),
    ]
    conn.executemany(
        "INSERT INTO runs (id, question_id, status, degraded, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        runs,
    )
    draws = [
        (1, MODEL_A, "2026-10-25T10:00:00Z"),
        (1, MODEL_B, "2026-10-25T10:00:00Z"),
        (2, MODEL_A, "2026-10-24T10:00:00Z"),
        (2, MODEL_C, "2026-10-24T10:00:00Z"),
        (5, MODEL_A, "2026-10-15T10:00:00Z"),
    ]
    conn.executemany(
        "INSERT INTO draws (run_id, model_id, weight, pool_avg, drawn_at) "
        "VALUES (?, ?, 1.0, 1.0, ?)",
        draws,
    )
    attempts = [
        (1, MODEL_A, 1, "2026-10-25T10:01:00Z", "ok", 0.30),
        (1, MODEL_B, 1, "2026-10-25T10:01:00Z", "ok", 0.20),
        (2, MODEL_A, 1, "2026-10-24T10:01:00Z", "failed", 0.05),
        (2, MODEL_A, 2, "2026-10-24T10:02:00Z", "ok", 0.25),
        (2, MODEL_C, 1, "2026-10-24T10:01:00Z", "failed", None),
        (5, MODEL_A, 1, "2026-10-15T10:01:00Z", "ok", 9.0),
    ]
    conn.executemany(
        "INSERT INTO model_forecasts "
        "(run_id, model_id, attempt, started_at, status, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        attempts,
    )
    conn.executemany(
        "INSERT INTO asknews_calls (run_id, calls, recorded_at) VALUES (?, ?, ?)",
        [
            (1, 30, "2026-10-23T10:00:00Z"),
            (2, 20, "2026-10-22T01:00:00Z"),
            (5, 99, "2026-10-21T23:59:59Z"),
        ],
    )
    conn.commit()
    conn.close()
    write_weights(
        data_dir,
        {MODEL_A: 1.5, MODEL_B: 0.5},
        {MODEL_A: 12, MODEL_B: 3},
        "2026-10-26T00:00:00Z",
    )
    archive = tmp_path / "state.tar.gz"
    build_snapshot_archive(data_dir, tmp_path / "no-iw.sqlite", archive)
    return encrypt_file(archive, KEY)


def _entry(username: str, rank: int, score: float, coverage: float) -> dict[str, Any]:
    return {
        "user": {"username": username},
        "rank": rank,
        "score": score,
        "coverage": coverage,
    }


def _post(forecast_at: datetime | None) -> dict[str, Any]:
    latest = {} if forecast_at is None else {"start_time": forecast_at.timestamp()}
    return {"question": {"my_forecasts": {"latest": latest}}}


def _posts(count: int) -> dict[str, Any]:
    return {"results": [_post(None) for _ in range(count)]}


def _mock_posts(
    httpx_mock: HTTPXMock, token: str | None, response: dict[str, Any], **params: Any
) -> None:
    httpx_mock.add_response(
        url=f"{METACULUS}/posts/",
        match_params={"limit": "100", "offset": "0", **params},
        match_headers={"Authorization": f"Token {token}"},
        json=response,
    )


def _mock_world(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    *,
    fail: frozenset[str] = frozenset(),
    watch_assets: bool = True,
    key_data: dict[str, float] | None = None,
) -> None:
    """Mock every source with a week that has numbers worth asserting.

    Args:
        httpx_mock: The mock to register on.
        tmp_path: Where the snapshot is built.
        fail: Source labels that answer 500 instead of their data.
        watch_assets: False leaves the watch release without a state asset.
        key_data: Overrides OpenRouter's `data` payload.
    """

    def add(label: str, **response: Any) -> None:
        if label in fail:
            kept = {k: v for k, v in response.items() if k not in ("json", "content")}
            httpx_mock.add_response(status_code=500, **kept)
        else:
            httpx_mock.add_response(**response)

    watch_state = (
        [{"id": 9, "name": "watch-1.json", "state": "uploaded"}] if watch_assets else []
    )
    httpx_mock.add_response(
        url=RELEASES,
        json=[
            {
                "id": 10,
                "tag_name": "state",
                "assets": [{"id": 7, "name": "state-1.enc", "state": "uploaded"}],
            },
            {"id": 55, "tag_name": "watch", "assets": watch_state},
        ],
        is_reusable=True,
    )
    add(
        "snapshot",
        url=f"{GITHUB}/repos/{REPO}/releases/assets/7",
        content=_seed_snapshot(tmp_path),
    )
    if watch_assets:
        httpx_mock.add_response(
            url=f"{GITHUB}/repos/{REPO}/releases/assets/9",
            content=orjson.dumps({"sent": [], "rounds": [33122, 33132]}),
        )
    fresh = {"updated_at": "2026-10-26T06:50:00Z", "status": "completed"}
    httpx_mock.add_response(
        url=f"{GITHUB}/repos/{REPO}/actions/workflows/host.yml/runs?per_page=20",
        json={"workflow_runs": [{"id": 900, "conclusion": "success", **fresh}]},
    )
    httpx_mock.add_response(
        url=(
            f"{GITHUB}/repos/Enhso/vezocontrol/actions/workflows/"
            "run_bot_on_tournament.yaml/runs?per_page=20"
        ),
        json={
            "workflow_runs": [
                {
                    "id": 71,
                    "status": "completed",
                    "conclusion": "success",
                    "updated_at": "2026-10-26T04:00:00Z",
                    "html_url": "https://github.com/Enhso/vezocontrol/actions/runs/71",
                }
            ]
        },
    )
    add(
        "pulls",
        url=f"{GITHUB}/repos/{REPO}/pulls?state=open",
        json=[
            {
                "number": 2,
                "title": "Weekly model check",
                "head": {"sha": "abc123"},
                "html_url": f"https://github.com/{REPO}/pull/2",
            }
        ],
    )
    add(
        "commits",
        url=f"{GITHUB}/repos/{REPO}/commits",
        match_params={"path": "config/models.yaml", "since": "2026-10-19T07:00:05Z"},
        json=[
            {
                "sha": "abcdef1234567890",
                "commit": {
                    "message": "config: weekly model check 2026-10-26\n\nbody",
                    "committer": {"date": "2026-10-26T06:20:00Z"},
                },
            }
        ],
    )
    add(
        "openrouter",
        url="https://openrouter.ai/api/v1/key",
        json={
            "data": key_data
            or {
                "limit_remaining": 70.0,
                "byok_usage_daily": 1.0,
                "byok_usage_weekly": 14.0,
            }
        },
    )
    fe_fall = [
        {
            "finalized": False,
            "entries": [
                _entry("someone", 1, 900.0, 1.0),
                _entry("vezo3", 12, 150.5, 0.9),
                _entry("vezocontrol", 40, -20.25, 0.5),
            ],
        }
    ]
    add("fe_fall", url=f"{METACULUS}/leaderboards/project/33121/", json=fe_fall)
    if watch_assets:
        httpx_mock.add_response(
            url=f"{METACULUS}/leaderboards/project/33122/",
            json=[
                {
                    "finalized": True,
                    "entries": [
                        _entry("vezo3", 49, 272.0, 0.98),
                        _entry("vezocontrol", 186, -403.0, 0.56),
                    ],
                }
            ],
        )
    httpx_mock.add_response(
        url=f"{METACULUS}/projects/tournaments/minibench/", json={"id": 33132}
    )
    _mock_posts(httpx_mock, "v1-token", _posts(3), tournaments="33132")
    _mock_posts(
        httpx_mock, "v1-token", _posts(3), tournaments="33132", forecaster_id="308852"
    )
    _mock_posts(
        httpx_mock, "v2-token", _posts(2), tournaments="33132", forecaster_id="308853"
    )
    week = datetime(2026, 10, 25, 12, tzinfo=UTC)
    older = datetime(2026, 10, 12, 12, tzinfo=UTC)
    _mock_posts(
        httpx_mock,
        "v1-token",
        {"results": [_post(week), _post(week), _post(older)]},
        forecaster_id="308852",
        open_time__gt="2026-10-19",
    )
    _mock_posts(
        httpx_mock,
        "v2-token",
        {"results": [_post(week)]},
        forecaster_id="308853",
        open_time__gt="2026-10-19",
    )


def _sent_mail(httpx_mock: HTTPXMock) -> dict[str, Any]:
    (request,) = httpx_mock.get_requests(url=RESEND)
    return orjson.loads(request.content)


async def test_the_summary_mails_every_section_with_the_seeded_numbers(
    httpx_mock: HTTPXMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    _mock_world(httpx_mock, tmp_path)
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})

    await run_summary(_settings(), now=NOW)

    mail = _sent_mail(httpx_mock)
    assert mail["subject"] == "betomcat weekly summary 2026-10-26"
    assert mail["from"] == "betomcat <onboarding@resend.dev>"
    assert mail["to"] == "owner@example.org"
    text = mail["text"]
    assert "Questions forecast" in text
    assert text == _expected_text()
    assert "<h2" in mail["html"]
    assert "unavailable" not in text
    for secret in ("v1-token", "v2-token", "gh-token", "re_secret", "owner@"):
        assert secret not in caplog.text
    assert "summary: 8 sections, 0 unavailable" in caplog.text


def _expected_text() -> str:
    return """betomcat weekly summary 2026-10-26

== Waiting on you ==
PR #2 | Weekly model check https://github.com/Enhso/betomcat/pull/2
Alert | v2 chain stalled (run_bot_on_tournament.yaml)

== Standings ==
Measure | vezo3 | vezocontrol
FE Fall rank | 12 | 40
FE Fall score | 150.50 | -20.25
FE Fall coverage | 0.90 | 0.50
Round 33122 (final) rank | 49 | 186
Round 33122 (final) score | 272.00 | -403.00
Round 33122 (final) coverage | 0.98 | 0.56
Round 33132 forecast | 3 of 3 | 2 of 3
Questions forecast, 7 days | 2 | 1

== vezo3 runs, 7 days ==
Runs with degraded research: 1
Status | Runs
failed | 1
ok | 2
unfinished | 1

== vezo3 models, 7 days ==
Cost, all models: $0.80
Model | Draws | OK / attempts | Cost
openai/model-a | 2 | 2 / 3 | $0.60
openai/model-b | 1 | 1 / 1 | $0.20
openai/model-c | 1 | 0 / 1 | $0.00

== Model weights ==
Model | Weight | Resolved
openai/model-a | 1.50 | 12
openai/model-b | 0.50 | 3

== AskNews ==
50 of 750 calls used this cycle (since 2026-10-22).

== Credit ==
Remaining (limit_remaining) | $70.00
Spend (byok_usage_weekly) | $14.00
Burn per day (weekly / 7) | $2.00
Projected run-out | 2026-11-30
Against the last forecasting day | 37 days short
Last forecasting day | 2027-01-06

== Pool changes, 7 days ==
Date | Commit | Subject
2026-10-26 | abcdef1 | config: weekly model check 2026-10-26
"""


@pytest.mark.parametrize(
    ("failing", "unavailable_line", "unavailable_count"),
    [
        ("snapshot", "== vezo3 runs, 7 days ==\nunavailable\n", 4),
        ("snapshot", "== AskNews ==\nunavailable\n", 4),
        ("openrouter", "== Credit ==\nunavailable\n", 1),
        ("commits", "== Pool changes, 7 days ==\nunavailable\n", 1),
        ("fe_fall", "FE Fall rank | unavailable | unavailable\n", 6),
        ("pulls", "Open PRs | unavailable\n", 1),
    ],
)
async def test_a_failing_source_shows_unavailable_and_the_rest_still_sends(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    failing: str,
    unavailable_line: str,
    unavailable_count: int,
) -> None:
    _mock_world(httpx_mock, tmp_path, fail=frozenset({failing}))
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})

    await run_summary(_settings(), now=NOW)

    text = _sent_mail(httpx_mock)["text"]
    assert unavailable_line in text
    assert text.count("unavailable") == unavailable_count
    assert "Round 33122 (final) rank | 49 | 186" in text
    assert "Round 33132 forecast | 3 of 3 | 2 of 3" in text


async def test_every_source_failing_still_sends_a_summary_of_unavailables(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=re.compile(
            r"https://(www\.metaculus\.com|api\.github\.com|openrouter\.ai)/.*"
        ),
        status_code=500,
        is_reusable=True,
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})

    await run_summary(_settings(), now=NOW)

    text = _sent_mail(httpx_mock)["text"]
    assert "FE Fall rank | unavailable | unavailable" in text
    assert "Latest finalized round | unavailable | unavailable" in text
    assert "Round" not in text.replace("Latest finalized round", "")
    assert "Questions forecast, 7 days | unavailable | unavailable" in text
    for title in (
        "vezo3 runs, 7 days",
        "Model weights",
        "Credit",
        "Pool changes, 7 days",
    ):
        assert f"== {title} ==\nunavailable\n" in text


async def test_a_watch_state_without_a_finalized_round_says_none_on_record(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    _mock_world(httpx_mock, tmp_path, watch_assets=False)
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})

    await run_summary(_settings(), now=NOW)

    text = _sent_mail(httpx_mock)["text"]
    assert "Latest finalized round | none on record | none on record" in text


async def test_the_credit_section_projects_the_run_out_against_the_season_end(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    _mock_world(
        httpx_mock,
        tmp_path,
        key_data={"limit_remaining": 700.0, "byok_usage_weekly": 14.0},
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})

    await run_summary(_settings(), now=NOW)

    text = _sent_mail(httpx_mock)["text"]
    assert "Projected run-out | 2027-10-11\n" in text
    assert "Against the last forecasting day | 278 days to spare\n" in text


async def test_the_credit_section_handles_a_week_without_spend(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    _mock_world(
        httpx_mock,
        tmp_path,
        key_data={"limit_remaining": 70.0, "byok_usage_weekly": 0.0},
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})

    await run_summary(_settings(), now=NOW)

    text = _sent_mail(httpx_mock)["text"]
    assert "Burn per day (weekly / 7) | $0.00\n" in text
    assert "Projected run-out | no spend this week\n" in text


async def test_out_writes_plain_html_and_sends_nothing(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    _mock_world(httpx_mock, tmp_path)
    out = tmp_path / "summary.html"

    await run_summary(_settings(), out, now=NOW)

    html = out.read_text()
    assert httpx_mock.get_requests(url=RESEND) == []
    assert html.count("<h2") == 8
    assert html.count("<table") == 7
    assert "<style" not in html
    assert 'name="viewport"' in html
    assert "<td style=" in html
    assert "<td>150.50</td>" not in html
    assert ">150.50</td>" in html


async def test_the_snapshot_directory_is_deleted_afterwards(
    httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    class Recording(tempfile.TemporaryDirectory):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self.name)

    _mock_world(httpx_mock, tmp_path)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", Recording)

    await run_summary(_settings(), tmp_path / "out.html", now=NOW)

    assert created
    assert not any(Path(name).exists() for name in created)


async def test_sending_without_the_mail_settings_exits_before_any_request(
    httpx_mock: HTTPXMock,
) -> None:
    settings = _settings()
    no_mail = Settings(**{**settings.__dict__, "resend_api_key": None})

    with pytest.raises(SystemExit, match="RESEND_API_KEY and SUMMARY_TO"):
        await run_summary(no_mail, now=NOW)

    assert httpx_mock.get_requests() == []


def test_the_summary_command_writes_the_file_given_to_out(
    httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", "v1-token")
    monkeypatch.setenv("V2_METACULUS_TOKEN", "v2-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-secret")
    httpx_mock.add_response(
        url=re.compile(
            r"https://(www\.metaculus\.com|api\.github\.com|openrouter\.ai)/.*"
        ),
        status_code=500,
        is_reusable=True,
    )
    out = tmp_path / "review.html"

    assert main(["summary", "--out", str(out)]) == 0

    assert "Standings" in out.read_text()
    assert httpx_mock.get_requests(url=RESEND) == []
