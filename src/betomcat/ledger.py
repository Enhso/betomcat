"""The betomcat ledger: sqlite3 at `DATA_DIR/ledger.sqlite`.

Normalised tables record everything the daily weight job (a later chunk)
needs to recompute model performance, and everything the private comment
(`comment.py`) needs to render. Timestamps are ISO 8601 UTC text.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question_type TEXT NOT NULL,
    close_time TIMESTAMP,
    url TEXT,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    status TEXT,
    family_id TEXT,
    degraded INTEGER NOT NULL DEFAULT 0,
    dossier_id TEXT,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS draws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    model_id TEXT NOT NULL,
    weight REAL NOT NULL,
    pool_avg REAL NOT NULL,
    fallback_fired INTEGER NOT NULL DEFAULT 0,
    drawn_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS model_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    model_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status TEXT NOT NULL,
    forecast_json TEXT,
    rationale TEXT,
    cost_usd REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL,
    forecast_json TEXT,
    comment_posted INTEGER NOT NULL DEFAULT 0,
    submitted_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS draw_fallbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    first_model TEXT NOT NULL,
    occurred_at TIMESTAMP NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


@dataclass(frozen=True)
class RunHandle:
    """A started run, returned so callers can finish/annotate it."""

    id: int
    question_id: str


class Ledger:
    """The bot's per-question audit trail."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- questions ---------------------------------------------------------

    def upsert_question(
        self,
        question_id: str,
        title: str,
        question_type: str,
        close_time: str | None,
        url: str | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO questions
                (question_id, title, question_type, close_time, url, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(question_id) DO UPDATE SET
                title = excluded.title,
                question_type = excluded.question_type,
                close_time = excluded.close_time,
                url = excluded.url
            """,
            (question_id, title, question_type, close_time, url, _now_iso()),
        )
        self._conn.commit()

    def get_question(self, question_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM questions WHERE question_id = ?", (question_id,)
        ).fetchone()
        return _row_to_dict(row)

    def has_run(self, question_id: str) -> bool:
        """True if any run (finished or in-flight) already exists for this question."""
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE question_id = ? LIMIT 1", (question_id,)
        ).fetchone()
        return row is not None

    # -- runs ----------------------------------------------------------------

    def start_run(self, question_id: str) -> RunHandle:
        cursor = self._conn.execute(
            "INSERT INTO runs (question_id, started_at) VALUES (?, ?)",
            (question_id, _now_iso()),
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return RunHandle(id=cursor.lastrowid, question_id=question_id)

    def finish_run(
        self,
        run_id: int,
        status: str,
        family_id: str | None = None,
        degraded: bool = False,
        dossier_id: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs
            SET status = ?, family_id = ?, degraded = ?, dossier_id = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, family_id, int(degraded), dossier_id, _now_iso(), run_id),
        )
        self._conn.commit()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_dict(row)

    # -- draws -----------------------------------------------------------------

    def record_draw(
        self,
        run_id: int,
        weights: dict[str, float],
        pool_avg: float,
        fallback_fired: bool,
    ) -> None:
        """One row per drawn model. `weights` order determines draw order."""
        drawn_at = _now_iso()
        self._conn.executemany(
            """
            INSERT INTO draws
                (run_id, model_id, weight, pool_avg, fallback_fired, drawn_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (run_id, model_id, weight, pool_avg, int(fallback_fired), drawn_at)
                for model_id, weight in weights.items()
            ],
        )
        self._conn.commit()

    def get_draws(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM draws WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_fallback(self, run_id: int, first_model: str) -> None:
        self._conn.execute(
            "INSERT INTO draw_fallbacks (run_id, first_model, occurred_at) "
            "VALUES (?, ?, ?)",
            (run_id, first_model, _now_iso()),
        )
        self._conn.commit()

    def get_fallbacks(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM draw_fallbacks WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- model forecasts ---------------------------------------------------

    def record_model_forecast(
        self,
        run_id: int,
        model_id: str,
        attempt: int,
        started_at: str,
        finished_at: str | None,
        status: str,
        forecast: object,
        rationale: str | None,
        cost_usd: float | None,
        tokens_in: int | None,
        tokens_out: int | None,
        error: str | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO model_forecasts
                (run_id, model_id, attempt, started_at, finished_at, status,
                 forecast_json, rationale, cost_usd, tokens_in, tokens_out, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                model_id,
                attempt,
                started_at,
                finished_at,
                status,
                orjson.dumps(forecast).decode() if forecast is not None else None,
                rationale,
                cost_usd,
                tokens_in,
                tokens_out,
                error,
            ),
        )
        self._conn.commit()

    def get_model_forecasts(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM model_forecasts WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_model_forecasts_for_question(
        self, question_id: str
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT model_forecasts.*
            FROM model_forecasts
            JOIN runs ON runs.id = model_forecasts.run_id
            WHERE runs.question_id = ?
            ORDER BY model_forecasts.id
            """,
            (question_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- submissions --------------------------------------------------------

    def record_submission(
        self,
        run_id: int,
        kind: str,
        forecast: object,
        comment_posted: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO submissions
                (run_id, kind, forecast_json, comment_posted, submitted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                orjson.dumps(forecast).decode() if forecast is not None else None,
                int(comment_posted),
                _now_iso(),
            ),
        )
        self._conn.commit()

    def get_submissions(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM submissions WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]
