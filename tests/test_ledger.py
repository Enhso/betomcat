"""Ledger round-trip tests (sqlite3, one row per drawn model/attempt/submission)."""

from __future__ import annotations

from pathlib import Path

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
        ledger.record_submission(run.id, "final", 0.555, comment_posted=True)

        submissions = ledger.get_submissions(run.id)
        assert [s["kind"] for s in submissions] == ["provisional", "final"]
        assert all(s["comment_posted"] == 1 for s in submissions)
    finally:
        ledger.close()


def test_ledger_as_context_manager(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        ledger.upsert_question("metaculus:1", "Will X?", "binary", None, None)
        assert ledger.get_question("metaculus:1") is not None


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
