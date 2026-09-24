"""Ledger round-trip tests (sqlite3, one row per drawn model/attempt/submission)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import orjson
import pytest

from betomcat.ledger import Ledger


def test_question_upsert_and_get(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question(
            "metaculus:1", "Will X?", "binary", "2026-09-23T00:00:00Z", "https://x"
        )
        question = ledger.get_question("metaculus:1")
        assert question is not None
        assert question["title"] == "Will X?"

        # Upsert again with a new title -- should update in place.
        ledger.upsert_question(
            "metaculus:1",
            "Will X happen?",
            "binary",
            "2026-09-23T00:00:00Z",
            "https://x",
        )
        question = ledger.get_question("metaculus:1")
        assert question is not None
        assert question["title"] == "Will X happen?"
    finally:
        ledger.close()


def test_has_run(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        assert ledger.has_run("metaculus:1") is False

        ledger.start_run("metaculus:1")
        assert ledger.has_run("metaculus:1") is True
    finally:
        ledger.close()


def test_run_lifecycle(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")
        assert run.question_id == "metaculus:1"

        row = ledger.get_run(run.id)
        assert row is not None
        assert row["status"] is None
        assert row["finished_at"] is None

        ledger.finish_run(
            run.id,
            status="submitted",
            family_id="fam:x",
            degraded=False,
            dossier_id="dos:1",
        )
        row = ledger.get_run(run.id)
        assert row is not None
        assert row["status"] == "submitted"
        assert row["family_id"] == "fam:x"
        assert row["degraded"] == 0
        assert row["dossier_id"] == "dos:1"
        assert row["finished_at"] is not None
    finally:
        ledger.close()


def test_draws_and_fallback_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")

        ledger.record_draw(
            run.id,
            weights={"model-a": 5.0, "model-b": 4.0},
            pool_avg=3.0,
            fallback_fired=True,
        )
        ledger.record_fallback(run.id, first_model="model-c")

        draws = ledger.get_draws(run.id)
        assert {d["model_id"] for d in draws} == {"model-a", "model-b"}
        assert all(d["fallback_fired"] == 1 for d in draws)
        assert all(d["pool_avg"] == 3.0 for d in draws)

        fallbacks = ledger.get_fallbacks(run.id)
        assert len(fallbacks) == 1
        assert fallbacks[0]["first_model"] == "model-c"
    finally:
        ledger.close()


def test_model_forecasts_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")

        ledger.record_model_forecast(
            run.id,
            "model-a",
            attempt=1,
            started_at="2026-09-22T19:00:00Z",
            finished_at="2026-09-22T19:00:05Z",
            status="ok",
            forecast=0.62,
            rationale="because X",
            cost_usd=0.01,
            tokens_in=100,
            tokens_out=50,
            error=None,
        )
        ledger.record_model_forecast(
            run.id,
            "model-b",
            attempt=1,
            started_at="2026-09-22T19:00:00Z",
            finished_at="2026-09-22T19:00:05Z",
            status="failed",
            forecast=None,
            rationale=None,
            cost_usd=None,
            tokens_in=None,
            tokens_out=None,
            error="timeout",
        )

        forecasts = ledger.get_model_forecasts(run.id)
        assert len(forecasts) == 2
        ok = next(f for f in forecasts if f["model_id"] == "model-a")
        assert ok["status"] == "ok"
        assert ok["forecast_json"] == "0.62"

        by_question = ledger.get_model_forecasts_for_question("metaculus:1")
        assert len(by_question) == 2
    finally:
        ledger.close()


def test_submissions_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")

        ledger.record_submission(run.id, "provisional", 0.62, comment_posted=True)
        ledger.record_submission(
            run.id, "final", 0.555, comment_posted=True, report="# full audit report"
        )

        submissions = ledger.get_submissions(run.id)
        assert [s["kind"] for s in submissions] == ["provisional", "final"]
        assert all(s["comment_posted"] == 1 for s in submissions)
        # `report` defaults to None when omitted, and is stored verbatim otherwise.
        assert submissions[0]["report"] is None
        assert submissions[1]["report"] == "# full audit report"
    finally:
        ledger.close()


def test_ledger_migrates_submissions_table_missing_report_column(
    tmp_path: Path,
) -> None:
    """A DB created before the `report` column existed must be migrated in place."""
    path = tmp_path / "ledger.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE questions (
            question_id TEXT PRIMARY KEY, title TEXT, question_type TEXT,
            close_time TIMESTAMP, url TEXT, created_at TIMESTAMP
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, question_id TEXT,
            status TEXT, family_id TEXT, degraded INTEGER, dossier_id TEXT,
            started_at TIMESTAMP, finished_at TIMESTAMP
        );
        CREATE TABLE submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, kind TEXT,
            forecast_json TEXT, comment_posted INTEGER, submitted_at TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()

    ledger = Ledger(path)
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")
        ledger.record_submission(run.id, "final", 0.5, comment_posted=True, report="r")

        submissions = ledger.get_submissions(run.id)
        assert submissions[0]["report"] == "r"
    finally:
        ledger.close()


def test_ledger_as_context_manager(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        assert ledger.get_question("metaculus:1") is not None


def test_pacing_decisions_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")

        ledger.record_pacing_decision(
            run.id, pace=1.7, daily_budget=8.0, excluded_ids=["model-a", "model-b"]
        )
        ledger.record_pacing_decision(
            run.id, pace=None, daily_budget=None, excluded_ids=[]
        )

        decisions = ledger.get_pacing_decisions(run.id)
        assert len(decisions) == 2
        assert decisions[0]["pace"] == pytest.approx(1.7)
        assert decisions[0]["daily_budget"] == pytest.approx(8.0)
        assert orjson.loads(decisions[0]["excluded_ids"]) == ["model-a", "model-b"]
        assert decisions[1]["pace"] is None
        assert orjson.loads(decisions[1]["excluded_ids"]) == []
    finally:
        ledger.close()


def test_get_recent_costs_returns_ok_calls_newest_first(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")

        for i, cost in enumerate([0.10, 0.20, 0.30]):
            ledger.record_model_forecast(
                run.id,
                "model-a",
                attempt=1,
                started_at="2026-09-22T19:00:00Z",
                finished_at="2026-09-22T19:00:05Z",
                status="ok",
                forecast=0.5,
                rationale=f"r{i}",
                cost_usd=cost,
                tokens_in=10,
                tokens_out=5,
                error=None,
            )
        # A failed call must not pollute the cost history.
        ledger.record_model_forecast(
            run.id,
            "model-a",
            attempt=2,
            started_at="2026-09-22T19:00:00Z",
            finished_at=None,
            status="failed",
            forecast=None,
            rationale=None,
            cost_usd=None,
            tokens_in=None,
            tokens_out=None,
            error="timeout",
        )

        costs = ledger.get_recent_costs("model-a", limit=10)
        assert costs == [0.30, 0.20, 0.10]

        assert ledger.get_recent_costs("model-a", limit=2) == [0.30, 0.20]
        assert ledger.get_recent_costs("model-unknown") == []
    finally:
        ledger.close()


def test_ledger_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger1 = Ledger(path)
    ledger1.upsert_question("metaculus:1", "Will X?", "binary", None, None)
    ledger1.close()

    ledger2 = Ledger(path)
    try:
        assert ledger2.get_question("metaculus:1") is not None
    finally:
        ledger2.close()
