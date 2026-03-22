from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nimble_reviewer.models import EnqueueDecision, MergeRequestState, ReviewRun, RunKind, TrackedFinding


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_merge_request_state_table(conn)
            self._ensure_review_run_table(conn)
            self._ensure_tracked_finding_table(conn)
            conn.commit()

    def enqueue_run(
        self,
        project_id: int,
        mr_iid: int,
        source_sha: str | None,
        target_sha: str | None,
        *,
        kind: RunKind = "full_review",
        trigger_discussion_id: str | None = None,
        trigger_note_id: int | None = None,
        trigger_author_id: int | None = None,
    ) -> EnqueueDecision:
        now = utcnow()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "full_review":
                duplicate = conn.execute(
                    """
                    SELECT id, status FROM review_run
                    WHERE project_id = ? AND mr_iid = ? AND kind = 'full_review'
                      AND source_sha IS ?
                      AND status IN ('queued', 'running', 'done')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (project_id, mr_iid, source_sha),
                ).fetchone()
                if duplicate:
                    self._upsert_mr_state_row(conn, project_id, mr_iid, source_sha, None, "duplicate", now)
                    conn.commit()
                    return EnqueueDecision(
                        enqueued=False,
                        reason=f"duplicate:{duplicate['status']}",
                        run_id=duplicate["id"],
                    )
            else:
                full_review = conn.execute(
                    """
                    SELECT id FROM review_run
                    WHERE project_id = ? AND mr_iid = ? AND kind = 'full_review'
                      AND status IN ('queued', 'running')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (project_id, mr_iid),
                ).fetchone()
                if full_review:
                    conn.commit()
                    return EnqueueDecision(
                        enqueued=False,
                        reason="full_review_pending",
                        run_id=full_review["id"],
                    )

            cursor = conn.execute(
                """
                INSERT INTO review_run(
                    project_id,
                    mr_iid,
                    kind,
                    source_sha,
                    target_sha,
                    status,
                    created_at,
                    trigger_discussion_id,
                    trigger_note_id,
                    trigger_author_id
                )
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    project_id,
                    mr_iid,
                    kind,
                    source_sha,
                    target_sha,
                    now,
                    trigger_discussion_id,
                    trigger_note_id,
                    trigger_author_id,
                ),
            )
            run_id = int(cursor.lastrowid)

            if kind == "full_review":
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
            else:
                conn.execute(
                    """
                    UPDATE review_run
                    SET status = 'superseded',
                        superseded_by = ?,
                        finished_at = COALESCE(finished_at, ?)
                    WHERE project_id = ? AND mr_iid = ? AND id <> ?
                      AND kind = 'discussion_reconcile'
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
                ORDER BY
                    CASE kind WHEN 'full_review' THEN 0 ELSE 1 END,
                    created_at ASC,
                    id ASC
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

    def mark_done(self, run_id: int, project_id: int, mr_iid: int, reviewed_sha: str | None) -> bool:
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

    def mark_failed(self, run_id: int, project_id: int, mr_iid: int, source_sha: str | None, error: str) -> bool:
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

    def get_latest_done_run(self, project_id: int, mr_iid: int, *, kind: RunKind = "full_review") -> ReviewRun | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM review_run
                WHERE project_id = ? AND mr_iid = ? AND kind = ? AND status = 'done'
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """,
                (project_id, mr_iid, kind),
            ).fetchone()
            return self._row_to_run(row) if row else None

    def update_summary_note_id(self, project_id: int, mr_iid: int, note_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE merge_request_state
                SET summary_note_id = ?, updated_at = ?
                WHERE project_id = ? AND mr_iid = ?
                """,
                (note_id, utcnow(), project_id, mr_iid),
            )
            conn.commit()

    def list_tracked_findings(
        self,
        project_id: int,
        mr_iid: int,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> list[TrackedFinding]:
        with closing(self._connect()) as conn:
            query = """
                SELECT * FROM tracked_finding
                WHERE project_id = ? AND mr_iid = ?
            """
            params: list[object] = [project_id, mr_iid]
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                query += f" AND status IN ({placeholders})"
                params.extend(statuses)
            query += " ORDER BY updated_at ASC, id ASC"
            rows = conn.execute(query, tuple(params)).fetchall()
            return [self._row_to_tracked_finding(row) for row in rows]

    def get_tracked_finding(self, project_id: int, mr_iid: int, fingerprint: str) -> TrackedFinding | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM tracked_finding
                WHERE project_id = ? AND mr_iid = ? AND fingerprint = ?
                """,
                (project_id, mr_iid, fingerprint),
            ).fetchone()
            return self._row_to_tracked_finding(row) if row else None

    def get_tracked_finding_by_discussion(self, project_id: int, mr_iid: int, discussion_id: str) -> TrackedFinding | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM tracked_finding
                WHERE project_id = ? AND mr_iid = ? AND discussion_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (project_id, mr_iid, discussion_id),
            ).fetchone()
            return self._row_to_tracked_finding(row) if row else None

    def upsert_tracked_finding(self, finding: TrackedFinding) -> None:
        now = utcnow()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO tracked_finding(
                    project_id,
                    mr_iid,
                    fingerprint,
                    status,
                    severity,
                    file,
                    line,
                    title,
                    body,
                    suggestion,
                    sources_json,
                    discussion_id,
                    root_note_id,
                    thread_owner,
                    opened_sha,
                    last_seen_sha,
                    resolved_sha,
                    dismissed_sha,
                    context_snippet,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, mr_iid, fingerprint) DO UPDATE SET
                    status = excluded.status,
                    severity = excluded.severity,
                    file = excluded.file,
                    line = excluded.line,
                    title = excluded.title,
                    body = excluded.body,
                    suggestion = excluded.suggestion,
                    sources_json = excluded.sources_json,
                    discussion_id = COALESCE(excluded.discussion_id, tracked_finding.discussion_id),
                    root_note_id = COALESCE(excluded.root_note_id, tracked_finding.root_note_id),
                    thread_owner = excluded.thread_owner,
                    opened_sha = COALESCE(tracked_finding.opened_sha, excluded.opened_sha),
                    last_seen_sha = excluded.last_seen_sha,
                    resolved_sha = excluded.resolved_sha,
                    dismissed_sha = excluded.dismissed_sha,
                    context_snippet = excluded.context_snippet,
                    updated_at = excluded.updated_at
                """,
                (
                    finding.project_id,
                    finding.mr_iid,
                    finding.fingerprint,
                    finding.status,
                    finding.severity,
                    finding.file,
                    finding.line,
                    finding.title,
                    finding.body,
                    finding.suggestion,
                    json.dumps(list(finding.sources)),
                    finding.discussion_id,
                    finding.root_note_id,
                    finding.thread_owner,
                    finding.opened_sha,
                    finding.last_seen_sha,
                    finding.resolved_sha,
                    finding.dismissed_sha,
                    finding.context_snippet,
                    finding.updated_at or now,
                ),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_merge_request_state_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merge_request_state (
                project_id INTEGER NOT NULL,
                mr_iid INTEGER NOT NULL,
                summary_note_id INTEGER,
                last_seen_sha TEXT,
                last_reviewed_sha TEXT,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, mr_iid)
            )
            """
        )
        columns = self._table_columns(conn, "merge_request_state")
        if "summary_note_id" not in columns:
            conn.execute("ALTER TABLE merge_request_state ADD COLUMN summary_note_id INTEGER")
        if "note_id" in columns:
            conn.execute(
                """
                UPDATE merge_request_state
                SET summary_note_id = COALESCE(summary_note_id, note_id)
                WHERE note_id IS NOT NULL
                """
            )

    def _ensure_review_run_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                mr_iid INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'full_review',
                source_sha TEXT,
                target_sha TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                superseded_by INTEGER,
                trigger_discussion_id TEXT,
                trigger_note_id INTEGER,
                trigger_author_id INTEGER
            )
            """
        )
        columns = self._table_columns(conn, "review_run")
        column_defs = {
            "kind": "TEXT NOT NULL DEFAULT 'full_review'",
            "trigger_discussion_id": "TEXT",
            "trigger_note_id": "INTEGER",
            "trigger_author_id": "INTEGER",
        }
        for column_name, definition in column_defs.items():
            if column_name not in columns:
                conn.execute(f"ALTER TABLE review_run ADD COLUMN {column_name} {definition}")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS review_run_status_created_idx
                ON review_run(status, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS review_run_project_mr_idx
                ON review_run(project_id, mr_iid, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS review_run_project_kind_status_idx
                ON review_run(project_id, mr_iid, kind, status, created_at)
            """
        )

    def _ensure_tracked_finding_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_finding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                mr_iid INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                file TEXT NOT NULL,
                line INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                suggestion TEXT,
                sources_json TEXT NOT NULL DEFAULT '[]',
                discussion_id TEXT,
                root_note_id INTEGER,
                thread_owner TEXT NOT NULL,
                opened_sha TEXT,
                last_seen_sha TEXT,
                resolved_sha TEXT,
                dismissed_sha TEXT,
                context_snippet TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, mr_iid, fingerprint)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS tracked_finding_project_status_idx
                ON tracked_finding(project_id, mr_iid, status, updated_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS tracked_finding_discussion_idx
                ON tracked_finding(project_id, mr_iid, discussion_id)
            """
        )

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

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
            INSERT INTO merge_request_state(project_id, mr_iid, summary_note_id, last_seen_sha, last_reviewed_sha, status, updated_at)
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
            kind=row["kind"],
            source_sha=row["source_sha"],
            target_sha=row["target_sha"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            superseded_by=row["superseded_by"],
            trigger_discussion_id=row["trigger_discussion_id"],
            trigger_note_id=row["trigger_note_id"],
            trigger_author_id=row["trigger_author_id"],
        )

    @staticmethod
    def _row_to_mr_state(row: sqlite3.Row) -> MergeRequestState:
        return MergeRequestState(
            project_id=row["project_id"],
            mr_iid=row["mr_iid"],
            summary_note_id=row["summary_note_id"],
            last_seen_sha=row["last_seen_sha"],
            last_reviewed_sha=row["last_reviewed_sha"],
            status=row["status"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_tracked_finding(row: sqlite3.Row) -> TrackedFinding:
        raw_sources = row["sources_json"] or "[]"
        try:
            parsed_sources = tuple(json.loads(raw_sources))
        except json.JSONDecodeError:
            parsed_sources = ()
        return TrackedFinding(
            project_id=row["project_id"],
            mr_iid=row["mr_iid"],
            fingerprint=row["fingerprint"],
            status=row["status"],
            severity=row["severity"],
            file=row["file"],
            line=row["line"],
            title=row["title"],
            body=row["body"],
            suggestion=row["suggestion"],
            sources=parsed_sources,  # type: ignore[arg-type]
            discussion_id=row["discussion_id"],
            root_note_id=row["root_note_id"],
            thread_owner=row["thread_owner"],
            opened_sha=row["opened_sha"],
            last_seen_sha=row["last_seen_sha"],
            resolved_sha=row["resolved_sha"],
            dismissed_sha=row["dismissed_sha"],
            context_snippet=row["context_snippet"],
            updated_at=row["updated_at"],
        )
