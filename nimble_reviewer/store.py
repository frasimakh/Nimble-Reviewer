from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nimble_reviewer.models import EnqueueDecision, MergeRequestState, ReviewRun


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS merge_request_state (
                    project_id INTEGER NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    note_id INTEGER,
                    last_seen_sha TEXT,
                    last_reviewed_sha TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, mr_iid)
                );
                CREATE TABLE IF NOT EXISTS review_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    source_sha TEXT NOT NULL,
                    target_sha TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    superseded_by INTEGER
                );
                CREATE INDEX IF NOT EXISTS review_run_status_created_idx
                    ON review_run(status, created_at);
                CREATE INDEX IF NOT EXISTS review_run_project_mr_idx
                    ON review_run(project_id, mr_iid, created_at);
                """
            )
            conn.commit()

    def enqueue_run(self, project_id: int, mr_iid: int, source_sha: str, target_sha: str | None) -> EnqueueDecision:
        now = utcnow()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, status FROM review_run
                WHERE project_id = ? AND mr_iid = ? AND source_sha = ?
                  AND status IN ('queued', 'running', 'done')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, mr_iid, source_sha),
            ).fetchone()
            if existing:
                self._upsert_mr_state_row(conn, project_id, mr_iid, source_sha, None, "duplicate", now)
                conn.commit()
                return EnqueueDecision(enqueued=False, reason=f"duplicate:{existing['status']}", run_id=existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO review_run(project_id, mr_iid, source_sha, target_sha, status, created_at)
                VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (project_id, mr_iid, source_sha, target_sha, now),
            )
            run_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE review_run
                SET status = 'superseded',
                    superseded_by = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE project_id = ? AND mr_iid = ? AND id <> ?
                  AND status IN ('queued', 'running')
                """,
                (run_id, now, project_id, mr_iid, run_id),
            )
            self._upsert_mr_state_row(conn, project_id, mr_iid, source_sha, None, "queued", now)
            conn.commit()
            return EnqueueDecision(enqueued=True, reason="queued", run_id=run_id)

    def claim_next_run(self) -> ReviewRun | None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM review_run
                WHERE status = 'queued'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                conn.commit()
                return None

            now = utcnow()
            updated = conn.execute(
                """
                UPDATE review_run
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, row["id"]),
            ).rowcount
            if updated != 1:
                conn.commit()
                return None

            claimed = conn.execute("SELECT * FROM review_run WHERE id = ?", (row["id"],)).fetchone()
            conn.commit()
            return self._row_to_run(claimed)

    def mark_done(self, run_id: int, project_id: int, mr_iid: int, reviewed_sha: str) -> bool:
        now = utcnow()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE review_run
                SET status = 'done', finished_at = ?, error = NULL
                WHERE id = ? AND status = 'running'
                """,
                (now, run_id),
            ).rowcount
            if changed:
                self._upsert_mr_state_row(conn, project_id, mr_iid, reviewed_sha, reviewed_sha, "done", now)
            conn.commit()
            return bool(changed)

    def mark_failed(self, run_id: int, project_id: int, mr_iid: int, source_sha: str, error: str) -> bool:
        now = utcnow()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE review_run
                SET status = 'failed', finished_at = ?, error = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, error, run_id),
            ).rowcount
            if changed:
                self._upsert_mr_state_row(conn, project_id, mr_iid, source_sha, None, "failed", now)
            conn.commit()
            return bool(changed)

    def mark_superseded_if_running(self, run_id: int) -> bool:
        with closing(self._connect()) as conn:
            changed = conn.execute(
                """
                UPDATE review_run
                SET status = 'superseded', finished_at = COALESCE(finished_at, ?)
                WHERE id = ? AND status = 'running'
                """,
                (utcnow(), run_id),
            ).rowcount
            conn.commit()
            return bool(changed)

    def get_run(self, run_id: int) -> ReviewRun | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM review_run WHERE id = ?", (run_id,)).fetchone()
            return self._row_to_run(row) if row else None

    def get_run_status(self, run_id: int) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT status FROM review_run WHERE id = ?", (run_id,)).fetchone()
            return row["status"] if row else None

    def get_merge_request_state(self, project_id: int, mr_iid: int) -> MergeRequestState | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM merge_request_state
                WHERE project_id = ? AND mr_iid = ?
                """,
                (project_id, mr_iid),
            ).fetchone()
            return self._row_to_mr_state(row) if row else None

    def update_note_id(self, project_id: int, mr_iid: int, note_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE merge_request_state
                SET note_id = ?, updated_at = ?
                WHERE project_id = ? AND mr_iid = ?
                """,
                (note_id, utcnow(), project_id, mr_iid),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _upsert_mr_state_row(
        conn: sqlite3.Connection,
        project_id: int,
        mr_iid: int,
        last_seen_sha: str | None,
        last_reviewed_sha: str | None,
        status: str,
        updated_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO merge_request_state(project_id, mr_iid, note_id, last_seen_sha, last_reviewed_sha, status, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(project_id, mr_iid) DO UPDATE SET
                last_seen_sha = excluded.last_seen_sha,
                last_reviewed_sha = COALESCE(excluded.last_reviewed_sha, merge_request_state.last_reviewed_sha),
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (project_id, mr_iid, last_seen_sha, last_reviewed_sha, status, updated_at),
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> ReviewRun:
        return ReviewRun(
            id=row["id"],
            project_id=row["project_id"],
            mr_iid=row["mr_iid"],
            source_sha=row["source_sha"],
            target_sha=row["target_sha"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            superseded_by=row["superseded_by"],
        )

    @staticmethod
    def _row_to_mr_state(row: sqlite3.Row) -> MergeRequestState:
        return MergeRequestState(
            project_id=row["project_id"],
            mr_iid=row["mr_iid"],
            note_id=row["note_id"],
            last_seen_sha=row["last_seen_sha"],
            last_reviewed_sha=row["last_reviewed_sha"],
            status=row["status"],
            updated_at=row["updated_at"],
        )
