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


def test_abandon_unfinished_runs_marks_only_null_status_runs(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        ledger.upsert_question("metaculus:2", "Will Y?", "binary", None, None)
        in_flight = ledger.start_run("metaculus:1")
        finished = ledger.start_run("metaculus:2")
        ledger.finish_run(finished.id, status="submitted")

        count = ledger.abandon_unfinished_runs()

        assert count == 1
        in_flight_row = ledger.get_run(in_flight.id)
        assert in_flight_row is not None
        assert in_flight_row["status"] == "abandoned"
        assert in_flight_row["finished_at"] is not None
        finished_row = ledger.get_run(finished.id)
        assert finished_row is not None
        assert finished_row["status"] == "submitted"
    finally:
        ledger.close()


def test_has_run_ignores_abandoned_runs_but_counts_every_other_status(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        for question_id in ("metaculus:1", "metaculus:2", "metaculus:3", "metaculus:4"):
            ledger.upsert_question(question_id, "Will X?", "binary", None, None)

        # Abandoned-only: must not block a retry.
        abandoned_only = ledger.start_run("metaculus:1")
        ledger.finish_run(abandoned_only.id, status="abandoned")
        assert ledger.has_run("metaculus:1") is False

        # Still NULL (in-flight): must keep blocking within the same shift.
        ledger.start_run("metaculus:2")
        assert ledger.has_run("metaculus:2") is True

        # Failed: not retried every poll.
        failed = ledger.start_run("metaculus:3")
        ledger.finish_run(failed.id, status="failed")
        assert ledger.has_run("metaculus:3") is True

        # Submitted: normal completed run.
        submitted = ledger.start_run("metaculus:4")
        ledger.finish_run(submitted.id, status="submitted")
        assert ledger.has_run("metaculus:4") is True
    finally:
        ledger.close()


def test_has_run_true_when_an_abandoned_run_has_a_newer_in_flight_sibling(
    tmp_path: Path,
) -> None:
    """A question with one abandoned run and one newer in-flight run (e.g. the
    abandoned attempt got retried this shift) must still block a second claim."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        old = ledger.start_run("metaculus:1")
        ledger.finish_run(old.id, status="abandoned")
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


def test_mark_comment_posted_flips_only_the_matching_submission(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        ledger.upsert_question("metaculus:2", "Will Y?", "binary", None, None)
        run = ledger.start_run("metaculus:1")
        other_run = ledger.start_run("metaculus:2")
        ledger.record_submission(run.id, "provisional", 0.62, comment_posted=False)
        ledger.record_submission(run.id, "final", 0.55, comment_posted=False)
        ledger.record_submission(other_run.id, "provisional", 0.3, comment_posted=False)

        ledger.mark_comment_posted(run.id, "provisional")

        submissions = ledger.get_submissions(run.id)
        assert [s["comment_posted"] for s in submissions] == [1, 0]
        assert ledger.get_submissions(other_run.id)[0]["comment_posted"] == 0
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


def test_round_start_is_recorded_once_and_never_overwritten(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        assert ledger.get_round_start("2026-10-05T00:00:00Z") is None

        first = ledger.record_round_start("2026-10-05T00:00:00Z", 95.0)
        second = ledger.record_round_start("2026-10-05T00:00:00Z", 80.0)

        assert first == pytest.approx(95.0)
        assert second == pytest.approx(95.0)
        assert ledger.get_round_start("2026-10-05T00:00:00Z") == pytest.approx(95.0)
    finally:
        ledger.close()


def test_round_starts_are_tracked_per_round(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        ledger.record_round_start("2026-09-21T00:00:00Z", 99.0)
        ledger.record_round_start("2026-10-05T00:00:00Z", 60.0)

        assert ledger.get_round_start("2026-09-21T00:00:00Z") == pytest.approx(99.0)
        assert ledger.get_round_start("2026-10-05T00:00:00Z") == pytest.approx(60.0)
    finally:
        ledger.close()


def test_round_start_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger1 = Ledger(path)
    ledger1.record_round_start("2026-10-05T00:00:00Z", 95.0)
    ledger1.close()

    ledger2 = Ledger(path)
    try:
        assert ledger2.record_round_start(
            "2026-10-05T00:00:00Z", 10.0
        ) == pytest.approx(95.0)
    finally:
        ledger2.close()


def test_ledger_adds_round_starts_table_to_a_snapshot_that_predates_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE round_starts")
    raw.commit()
    raw.close()

    ledger = Ledger(path)
    try:
        assert ledger.record_round_start("2026-10-05T00:00:00Z", 90.0) == pytest.approx(
            90.0
        )
    finally:
        ledger.close()


def test_asknews_calls_since_sums_only_calls_at_or_after_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        assert ledger.asknews_calls_since("2026-10-22T00:00:00Z") == 0
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        ledger.upsert_question("metaculus:2", "Will Y?", "binary", None, None)
        first_run = ledger.start_run("metaculus:1")
        second_run = ledger.start_run("metaculus:2")
        recorded = [
            ("2026-10-21T23:59:59Z", first_run.id, 5),
            ("2026-10-22T00:00:00Z", first_run.id, 7),
            ("2026-10-25T12:00:00Z", second_run.id, 11),
        ]
        for recorded_at, run_id, calls in recorded:
            with monkeypatch.context() as patch:
                patch.setattr("betomcat.ledger._now_iso", lambda at=recorded_at: at)
                ledger.record_asknews_calls(run_id, calls)

        assert ledger.asknews_calls_since("2026-10-22T00:00:00Z") == 18
        assert ledger.asknews_calls_since("2026-10-22T00:00:01Z") == 11
        assert ledger.asknews_calls_since("2026-10-21T00:00:00Z") == 23
        assert ledger.asknews_calls_since("2026-10-26T00:00:00Z") == 0
    finally:
        ledger.close()


def test_ledger_adds_asknews_calls_table_to_a_file_that_predates_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    old = Ledger(path)
    old.upsert_question("metaculus:1", "Will X?", "binary", None, None)
    run = old.start_run("metaculus:1")
    old.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE asknews_calls")
    raw.commit()
    tables_before = {name for (name,) in raw.execute("SELECT name FROM sqlite_master")}
    raw.close()
    assert "asknews_calls" not in tables_before

    ledger = Ledger(path)
    try:
        assert ledger.asknews_calls_since("2000-01-01T00:00:00Z") == 0
        ledger.record_asknews_calls(run.id, 3)
        assert ledger.asknews_calls_since("2000-01-01T00:00:00Z") == 3
        assert ledger.get_question("metaculus:1") is not None
    finally:
        ledger.close()

    reopened = Ledger(path)
    try:
        assert reopened.asknews_calls_since("2000-01-01T00:00:00Z") == 3
    finally:
        reopened.close()


def _record_forecast(
    ledger: Ledger, run_id: int, model_id: str, status: str, forecast: object
) -> None:
    ledger.record_model_forecast(
        run_id,
        model_id,
        attempt=1,
        started_at="2026-09-22T19:00:00Z",
        finished_at="2026-09-22T19:00:05Z",
        status=status,
        forecast=forecast,
        rationale=None,
        cost_usd=None,
        tokens_in=None,
        tokens_out=None,
        error=None,
    )


def test_ledger_adds_weight_tables_to_a_snapshot_that_predates_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE resolutions")
    raw.execute("DROP TABLE model_scores")
    raw.commit()
    raw.close()

    ledger = Ledger(path)
    try:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")
        assert ledger.get_resolution("metaculus:1") is None
        assert ledger.get_model_scores() == []
        ledger.upsert_resolution("metaculus:1", "pending", None, None, None, None, "t")
        assert ledger.record_model_score(run.id, "model-a", "metaculus:1", 0.5, "peer")
        assert ledger.get_resolution("metaculus:1") is not None
    finally:
        ledger.close()


def test_resolution_round_trip_and_overwrite(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)

        ledger.upsert_resolution(
            "metaculus:1", "pending", None, None, None, "2026-10-17T00:00:00Z", "t1"
        )
        pending = ledger.get_resolution("metaculus:1")
        assert pending == {
            "question_id": "metaculus:1",
            "status": "pending",
            "resolution": None,
            "field_log_score": None,
            "scaling_json": None,
            "scheduled_resolve_time": "2026-10-17T00:00:00Z",
            "fetched_at": "t1",
        }

        ledger.upsert_resolution(
            "metaculus:1", "resolved", "no", -0.17, {"type": "binary"}, None, "t2"
        )
        resolved = ledger.get_resolution("metaculus:1")
        assert resolved is not None
        assert resolved["status"] == "resolved"
        assert resolved["field_log_score"] == -0.17
        assert resolved["scaling_json"] == '{"type":"binary"}'
        assert resolved["fetched_at"] == "t2"


def test_model_score_is_unique_per_run_and_model(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        run = ledger.start_run("metaculus:1")
        record = ledger.record_model_score

        assert record(run.id, "model-a", "metaculus:1", 0.5, "baseline") == 1
        assert record(run.id, "model-a", "metaculus:1", 0.5, "baseline") == 0
        assert record(run.id, "model-b", "metaculus:1", -1.0, "baseline") == 1
        # A changed score or method replaces the row; the row stays unique.
        assert record(run.id, "model-a", "metaculus:1", 0.25, "peer") == 1
        assert record(run.id, "model-b", "metaculus:1", -2.0, "baseline") == 1

        scores = [
            (r["model_id"], r["score"], r["method"]) for r in ledger.get_model_scores()
        ]
        assert scores == [("model-a", 0.25, "peer"), ("model-b", -2.0, "baseline")]


def test_forecasted_questions_and_last_ok_forecasts(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        ledger.upsert_question("metaculus:2", "Will Y?", "binary", None, None)
        ledger.upsert_question("metaculus:3", "Will Z?", "binary", None, None)
        run1 = ledger.start_run("metaculus:1")
        run2 = ledger.start_run("metaculus:2")
        ledger.start_run("metaculus:3")  # no forecast at all
        _record_forecast(ledger, run1.id, "model-a", "ok", 0.9)
        _record_forecast(ledger, run1.id, "model-a", "ok", 0.2)
        _record_forecast(ledger, run1.id, "model-a", "failed", None)
        _record_forecast(ledger, run1.id, "model-b", "ok", 0.7)
        _record_forecast(ledger, run2.id, "model-a", "failed", None)

        assert [q["question_id"] for q in ledger.forecasted_questions()] == [
            "metaculus:1"
        ]
        last = ledger.last_ok_forecasts("metaculus:1")
        assert [(r["run_id"], r["model_id"], r["forecast_json"]) for r in last] == [
            (run1.id, "model-a", "0.2"),
            (run1.id, "model-b", "0.7"),
        ]
        assert ledger.last_ok_forecasts("metaculus:2") == []
