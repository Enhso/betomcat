"""Tests for watch.py: which alert keys fire, dedupe, and the watch state.

Every HTTP call is mocked through pytest_httpx; the tests call `run_watch` as
`betomcat watch` does.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
from pytest_httpx import HTTPXMock

from betomcat import sources
from betomcat.cli import main
from betomcat.config import Settings
from betomcat.watch import (
    Alert,
    WatchState,
    compose,
    run_watch,
    select_new,
)

NOW = datetime(2026, 10, 26, 7, 0, 5, tzinfo=UTC)
REPO = "Enhso/betomcat"
GITHUB = "https://api.github.com"
RELEASES = f"{GITHUB}/repos/{REPO}/releases?per_page=100"
HOST_RUNS = f"{GITHUB}/repos/{REPO}/actions/workflows/host.yml/runs?per_page=20"
CONTROL_RUNS = (
    f"{GITHUB}/repos/Enhso/vezocontrol/actions/workflows/"
    "run_bot_on_tournament.yaml/runs?per_page=20"
)
PULLS = f"{GITHUB}/repos/{REPO}/pulls?state=open"
KEY_URL = "https://openrouter.ai/api/v1/key"
MINIBENCH = "https://www.metaculus.com/api/projects/tournaments/minibench/"
RESEND = "https://api.resend.com/emails"
UPLOAD = f"https://uploads.github.com/repos/{REPO}/releases/55/assets"
SECRETS = ("gh-token-secret", "meta-token-secret", "re_secret_key", "owner@example.org")


def _leaderboard_url(project_id: int) -> str:
    return f"https://www.metaculus.com/api/leaderboards/project/{project_id}/"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "METACULUS_PAUSE_SECONDS", 0.0)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token-secret")


def _settings() -> Settings:
    return Settings(
        metaculus_token="meta-token-secret",
        openrouter_api_key="or-key-secret",
        openrouter_free_api_key=None,
        asknews_api_key=None,
        iw_url="http://127.0.0.1:8080",
        data_dir=Path("data"),
        soft_threshold_min=30,
        hard_threshold_min=5,
        poll_seconds=300,
        dry_run=False,
        resend_api_key="re_secret_key",
        summary_to="owner@example.org",
    )


def _run(
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    updated_at: str = "2026-10-26T06:50:00Z",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "updated_at": updated_at,
        "html_url": f"https://github.com/x/actions/runs/{run_id}",
    }


def _pull(number: int, sha: str, title: str = "Weekly model check") -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "head": {"sha": sha},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }


def _entry(username: str, rank: int, score: float, coverage: float) -> dict[str, Any]:
    return {
        "user": {"username": username},
        "rank": rank,
        "score": score,
        "coverage": coverage,
    }


def _board(finalized: bool) -> list[dict[str, Any]]:
    return [
        {
            "finalized": finalized,
            "entries": [
                _entry("someone", 1, 900.0, 1.0),
                _entry("vezo3", 49, 272.0, 0.98),
                _entry("vezocontrol", 186, -403.0, 0.56),
            ],
        }
    ]


def _state_json(sent: list[str], rounds: list[int]) -> bytes:
    return orjson.dumps({"sent": sent, "rounds": rounds})


def _world(
    httpx_mock: HTTPXMock,
    *,
    state: bytes | None = None,
    host_runs: Any = None,
    control_runs: Any = None,
    credit: float = 69.64,
    pulls: Any = None,
    current_round: int = 33132,
    boards: dict[int, bool] | None = None,
) -> None:
    """Mock every source; the defaults are a healthy world with no alerts."""
    assets = []
    if state is not None:
        assets = [
            {"id": 700, "name": "watch-20261026T060000Z.json", "state": "uploaded"}
        ]
        httpx_mock.add_response(
            url=f"{GITHUB}/repos/{REPO}/releases/assets/700", content=state
        )
    httpx_mock.add_response(
        url=RELEASES,
        json=[
            {"id": 10, "tag_name": "state", "assets": []},
            {"id": 55, "tag_name": "watch", "assets": assets},
        ],
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=HOST_RUNS,
        json={"workflow_runs": host_runs if host_runs is not None else [_run(900)]},
    )
    httpx_mock.add_response(
        url=CONTROL_RUNS,
        json={
            "workflow_runs": control_runs if control_runs is not None else [_run(70)]
        },
    )
    httpx_mock.add_response(url=KEY_URL, json={"data": {"limit_remaining": credit}})
    httpx_mock.add_response(url=PULLS, json=pulls if pulls is not None else [])
    httpx_mock.add_response(url=MINIBENCH, json={"id": current_round})
    for project_id, finalized in (boards or {current_round: False}).items():
        httpx_mock.add_response(
            url=_leaderboard_url(project_id), json=_board(finalized)
        )


def _mails(httpx_mock: HTTPXMock) -> list[dict[str, Any]]:
    return [
        orjson.loads(request.content) for request in httpx_mock.get_requests(url=RESEND)
    ]


def _uploads(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    return [
        request
        for request in httpx_mock.get_requests(method="POST")
        if request.url.host == "uploads.github.com"
    ]


def _uploaded_state(httpx_mock: HTTPXMock) -> dict[str, Any]:
    (request,) = _uploads(httpx_mock)
    return orjson.loads(request.content)


def _mock_state_write(httpx_mock: HTTPXMock, name: str | None = None) -> None:
    httpx_mock.add_response(
        method="POST",
        url=(
            f"{UPLOAD}?name={name}"
            if name
            else re.compile(rf"{UPLOAD}\?name=watch-.*\.json")
        ),
        json={"id": 701, "name": name or "watch-new.json"},
    )


async def test_first_run_seeds_the_state_and_mails_the_open_pr(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _world(httpx_mock, pulls=[_pull(2, "abc123")])
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock, "watch-20261026T070005Z.json")

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: PR #2 waiting: Weekly model check"
    assert mail["from"] == "betomcat <onboarding@resend.dev>"
    assert mail["to"] == "owner@example.org"
    assert "https://github.com/Enhso/betomcat/pull/2" in mail["text"]
    assert _uploaded_state(httpx_mock) == {"sent": ["pr-2-abc123"], "rounds": [33132]}
    for secret in SECRETS:
        assert secret not in caplog.text
    assert "watch: 1 active, 1 new" in caplog.text


async def test_a_key_already_sent_is_not_mailed_again(httpx_mock: HTTPXMock) -> None:
    _world(
        httpx_mock,
        state=_state_json(["pr-2-abc123"], [33132]),
        pulls=[_pull(2, "abc123")],
    )

    await run_watch(_settings(), now=NOW)

    assert _mails(httpx_mock) == []
    assert httpx_mock.get_requests(method="POST") == []


async def test_a_force_push_with_a_new_head_sha_alerts_again(
    httpx_mock: HTTPXMock,
) -> None:
    _world(
        httpx_mock,
        state=_state_json(["pr-2-abc123"], [33132]),
        pulls=[_pull(2, "def456")],
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: PR #2 waiting: Weekly model check"
    assert _uploaded_state(httpx_mock) == {
        "sent": ["pr-2-abc123", "pr-2-def456"],
        "rounds": [33132],
    }


async def test_one_mail_lists_every_new_alert_and_names_the_failed_step(
    httpx_mock: HTTPXMock,
) -> None:
    _world(
        httpx_mock,
        state=_state_json([], [33132]),
        host_runs=[_run(900, conclusion="failure", updated_at="2026-10-26T06:40:00Z")],
        credit=3.2,
    )
    httpx_mock.add_response(
        url=f"{GITHUB}/repos/{REPO}/actions/runs/900/jobs",
        json={
            "jobs": [
                {
                    "name": "host",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Sync betomcat dependencies", "conclusion": "success"},
                        {"name": "Test gate", "conclusion": "failure"},
                        {"name": "Run host shift", "conclusion": "skipped"},
                    ],
                }
            ]
        },
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: host.yml run 900 failed (+2 more)"
    assert "Failed step: host: Test gate" in mail["text"]
    assert "credit below $15 ($3.20 left)" in mail["text"]
    assert "credit below $5 ($3.20 left)" in mail["text"]
    assert _uploaded_state(httpx_mock)["sent"] == [
        "credit-15",
        "credit-5",
        "failed-900",
    ]


async def test_a_dead_v1_chain_alerts_once_per_last_run(httpx_mock: HTTPXMock) -> None:
    _world(
        httpx_mock,
        state=_state_json([], [33132]),
        host_runs=[_run(801, updated_at="2026-10-26T05:00:00Z"), _run(800)],
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: v1 chain stalled (host.yml)"
    assert "The newest, run 801, finished 120 min ago (success)." in mail["text"]
    assert _uploaded_state(httpx_mock)["sent"] == ["chain-v1-801"]


async def test_a_dead_v2_chain_reads_the_control_repo(httpx_mock: HTTPXMock) -> None:
    _world(
        httpx_mock,
        state=_state_json(["chain-v1-1"], [33132]),
        control_runs=[_run(71, updated_at="2026-10-26T04:00:00Z")],
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: v2 chain stalled (run_bot_on_tournament.yaml)"
    assert (
        "No run_bot_on_tournament.yaml run is queued or in progress on" in mail["text"]
    )
    assert _uploaded_state(httpx_mock)["sent"] == ["chain-v1-1", "chain-v2-71"]


@pytest.mark.parametrize(
    "runs",
    [
        [_run(802, status="in_progress", conclusion=None), _run(801)],
        [_run(802, status="queued", conclusion=None), _run(801)],
        [_run(801, updated_at="2026-10-26T06:16:00Z")],
    ],
    ids=["in-progress", "queued", "finished-44-minutes-ago"],
)
async def test_a_live_or_recent_chain_does_not_alert(
    httpx_mock: HTTPXMock, runs: list[dict[str, Any]]
) -> None:
    _world(httpx_mock, state=_state_json([], [33132]), host_runs=runs)

    await run_watch(_settings(), now=NOW)

    assert _mails(httpx_mock) == []


async def test_credit_alerts_at_15_and_again_at_5(httpx_mock: HTTPXMock) -> None:
    _world(httpx_mock, state=_state_json(["credit-15"], [33132]), credit=4.5)
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: credit below $5 ($4.50 left)"
    assert _uploaded_state(httpx_mock)["sent"] == ["credit-15", "credit-5"]


async def test_credit_back_at_15_clears_both_keys_without_a_mail(
    httpx_mock: HTTPXMock,
) -> None:
    _world(
        httpx_mock,
        state=_state_json(["credit-15", "credit-5", "pr-2-abc"], [33132]),
        credit=15.0,
        pulls=[_pull(2, "abc")],
    )
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    assert _mails(httpx_mock) == []
    assert _uploaded_state(httpx_mock) == {"sent": ["pr-2-abc"], "rounds": [33132]}


def test_a_credit_drop_after_the_reset_alerts_again() -> None:
    state = WatchState(frozenset({"credit-15", "credit-5"}), (33132,))
    low = [Alert("credit-15", "credit below $15 ($12.00 left)", ("x",))]

    _, cleared = select_new([], state, 20.0)
    new, _ = select_new(low, cleared, 12.0)
    stale_new, _ = select_new(low, state, 12.0)

    assert cleared.sent == frozenset()
    assert [a.key for a in new] == ["credit-15"]
    assert stale_new == []


async def test_a_finalized_round_alerts_once_with_both_bots(
    httpx_mock: HTTPXMock,
) -> None:
    _world(
        httpx_mock,
        state=_state_json([], [33132]),
        boards={33132: True},
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: MiniBench round 33132 finalized"
    assert "vezo3: rank 49, score 272.00, coverage 0.98" in mail["text"]
    assert "vezocontrol: rank 186, score -403.00, coverage 0.56" in mail["text"]
    assert _uploaded_state(httpx_mock) == {"sent": ["round-33132"], "rounds": [33132]}


async def test_a_round_not_finalized_yet_stays_quiet_and_an_alerted_one_is_skipped(
    httpx_mock: HTTPXMock,
) -> None:
    _world(
        httpx_mock,
        state=_state_json(["round-33122"], [33122, 33132]),
        boards={33132: False},
    )

    await run_watch(_settings(), now=NOW)

    assert _mails(httpx_mock) == []
    assert not httpx_mock.get_requests(url=_leaderboard_url(33122))


async def test_a_new_round_is_recorded_and_the_old_one_still_checked(
    httpx_mock: HTTPXMock,
) -> None:
    _world(
        httpx_mock,
        state=_state_json([], [33132]),
        current_round=33140,
        boards={33132: True, 33140: False},
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: MiniBench round 33132 finalized"
    assert _uploaded_state(httpx_mock) == {
        "sent": ["round-33132"],
        "rounds": [33132, 33140],
    }


@pytest.mark.parametrize(
    "release_response",
    [
        {"status_code": 500, "text": "boom"},
        {
            "json": [
                {
                    "id": 55,
                    "tag_name": "watch",
                    "assets": [
                        {"id": 700, "name": "watch-1.json", "state": "uploaded"}
                    ],
                }
            ]
        },
    ],
    ids=["list-fails", "asset-download-fails"],
)
async def test_a_state_read_failure_sends_nothing_and_writes_nothing(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
    release_response: dict[str, Any],
) -> None:
    httpx_mock.add_response(url=RELEASES, **release_response)
    if "json" in release_response:
        httpx_mock.add_response(
            url=f"{GITHUB}/repos/{REPO}/releases/assets/700", status_code=502
        )

    await run_watch(_settings(), now=NOW)

    methods_and_hosts = {(r.method, r.url.host) for r in httpx_mock.get_requests()}
    assert methods_and_hosts <= {("GET", "api.github.com")}, (
        "only the state read may happen"
    )
    assert not httpx_mock.get_requests(url=RESEND)
    assert "watch state read failed" in caplog.text
    assert "sending nothing" in caplog.text


async def test_a_malformed_state_asset_sends_nothing(httpx_mock: HTTPXMock) -> None:
    malformed = {"sent": "not-a-list", "rounds": [33132]}
    httpx_mock.add_response(
        url=RELEASES,
        json=[
            {
                "id": 55,
                "tag_name": "watch",
                "assets": [{"id": 700, "name": "watch-1.json", "state": "uploaded"}],
            }
        ],
    )
    httpx_mock.add_response(
        url=f"{GITHUB}/repos/{REPO}/releases/assets/700", json=malformed
    )

    await run_watch(_settings(), now=NOW)

    assert len(httpx_mock.get_requests()) == 2
    assert not httpx_mock.get_requests(url=RESEND)


async def test_a_failed_source_suppresses_its_condition_and_never_fires_it(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=RELEASES,
        json=[
            {
                "id": 55,
                "tag_name": "watch",
                "assets": [{"id": 700, "name": "watch-1.json", "state": "uploaded"}],
            }
        ],
    )
    httpx_mock.add_response(
        url=f"{GITHUB}/repos/{REPO}/releases/assets/700",
        content=_state_json([], [33132]),
    )
    for url in (HOST_RUNS, CONTROL_RUNS, PULLS, KEY_URL, MINIBENCH):
        httpx_mock.add_response(url=url, status_code=500)
    httpx_mock.add_response(url=_leaderboard_url(33132), status_code=500)

    await run_watch(_settings(), now=NOW)

    assert not httpx_mock.get_requests(url=RESEND)
    assert not httpx_mock.get_requests(method="POST")


async def test_a_failed_send_stores_no_keys_so_the_next_run_retries(
    httpx_mock: HTTPXMock,
) -> None:
    _world(httpx_mock, state=_state_json([], [33132]), pulls=[_pull(2, "abc")])
    httpx_mock.add_response(url=RESEND, method="POST", status_code=500)

    with pytest.raises(httpx.HTTPStatusError):
        await run_watch(_settings(), now=NOW)

    assert _uploads(httpx_mock) == []


async def test_the_state_asset_is_rotated_and_the_state_release_left_alone(
    httpx_mock: HTTPXMock,
) -> None:
    names = [f"watch-2026102{n}T060000Z.json" for n in range(1, 4)]
    httpx_mock.add_response(
        url=RELEASES,
        json=[
            {
                "id": 10,
                "tag_name": "state",
                "assets": [{"id": 5, "name": "state-1.enc", "state": "uploaded"}],
            },
            {
                "id": 55,
                "tag_name": "watch",
                "assets": [
                    {"id": 600 + i, "name": name, "state": "uploaded"}
                    for i, name in enumerate(names)
                ],
            },
        ],
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=f"{GITHUB}/repos/{REPO}/releases/assets/602",
        content=_state_json([], [33132]),
    )
    for url, payload in (
        (HOST_RUNS, {"workflow_runs": [_run(900)]}),
        (CONTROL_RUNS, {"workflow_runs": [_run(70)]}),
        (KEY_URL, {"data": {"limit_remaining": 50}}),
        (PULLS, [_pull(2, "abc")]),
        (MINIBENCH, {"id": 33132}),
        (_leaderboard_url(33132), _board(False)),
    ):
        httpx_mock.add_response(url=url, json=payload)
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock, "watch-20261026T070005Z.json")
    httpx_mock.add_response(
        method="DELETE",
        url=f"{GITHUB}/repos/{REPO}/releases/assets/600",
        status_code=204,
    )

    await run_watch(_settings(), now=NOW)

    deleted = [r.url.path for r in httpx_mock.get_requests(method="DELETE")]
    assert deleted == [f"/repos/{REPO}/releases/assets/600"]
    assert not httpx_mock.get_requests(url=f"{GITHUB}/repos/{REPO}/releases/assets/5")


def test_compose_puts_the_first_alert_in_the_subject_and_lists_them_all() -> None:
    alerts = [
        Alert("a", "first <thing>", ("line one",)),
        Alert("b", "second", ("line two",)),
        Alert("c", "third", ("line three",)),
    ]

    subject, html, text = compose(alerts)

    assert subject == "betomcat: first <thing> (+2 more)"
    assert "first &lt;thing&gt;" in html
    assert "<style" not in html
    assert text.splitlines()[0] == "betomcat alerts"
    assert "== second ==\nline two" in text


async def test_the_watch_needs_the_github_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")

    with pytest.raises(SystemExit, match="GITHUB_REPOSITORY and GITHUB_TOKEN"):
        await run_watch(_settings(), now=NOW)


def test_the_watch_command_runs_through_the_cli_and_survives_a_state_read_failure(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_secret_key")
    monkeypatch.setenv("SUMMARY_TO", "owner@example.org")
    httpx_mock.add_response(url=RELEASES, status_code=500)

    assert main(["watch"]) == 0

    assert len(httpx_mock.get_requests()) == 1
    assert httpx_mock.get_requests(url=RESEND) == []


@pytest.mark.parametrize(
    ("jobs_response", "expected_line"),
    [
        ({"status_code": 500}, "Failed step: unknown"),
        (
            {
                "json": {
                    "jobs": [{"name": "host", "conclusion": "failure", "steps": []}]
                }
            },
            "Failed step: host",
        ),
    ],
    ids=["jobs-unreadable", "job-failed-before-any-step"],
)
async def test_a_failed_run_still_alerts_without_a_named_step(
    httpx_mock: HTTPXMock, jobs_response: dict[str, Any], expected_line: str
) -> None:
    _world(
        httpx_mock,
        state=_state_json([], [33132]),
        host_runs=[_run(900, conclusion="failure", updated_at="2026-10-26T06:40:00Z")],
    )
    httpx_mock.add_response(
        url=f"{GITHUB}/repos/{REPO}/actions/runs/900/jobs", **jobs_response
    )
    httpx_mock.add_response(url=RESEND, method="POST", json={"id": "m1"})
    _mock_state_write(httpx_mock)

    await run_watch(_settings(), now=NOW)

    (mail,) = _mails(httpx_mock)
    assert mail["subject"] == "betomcat: host.yml run 900 failed"
    assert expected_line in mail["text"]
    assert _uploaded_state(httpx_mock)["sent"] == ["failed-900"]
