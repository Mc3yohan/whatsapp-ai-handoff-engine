# -*- coding: utf-8 -*-
"""SQLite WAL durable state store for sessions, wamid idempotency, and audit logs."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.models import AuditEventOut, ConversationSession, ConversationState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteHandoffStore:
    """Thread-safe SQLite store (WAL mode for file DBs, shared connection for :memory:)."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    wa_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    assigned_agent TEXT,
                    handoff_reason TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    wa_id TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wa_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_wa_id ON audit_events(wa_id, id);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row_to_session(self, row: sqlite3.Row) -> ConversationSession:
        return ConversationSession(
            wa_id=row["wa_id"],
            state=ConversationState(row["state"]),
            assigned_agent=row["assigned_agent"],
            handoff_reason=row["handoff_reason"],
            prompt_tokens_used=int(row["prompt_tokens"]),
            completion_tokens_used=int(row["completion_tokens"]),
            updated_at=row["updated_at"],
        )

    def get_session(self, wa_id: str) -> Optional[ConversationSession]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE wa_id = ?",
                (wa_id,),
            ).fetchone()
            return self._row_to_session(row) if row else None

    def get_or_create_session(self, wa_id: str) -> ConversationSession:
        with self._lock:
            existing = self.get_session(wa_id)
            if existing is not None:
                return existing
            now = _utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO sessions (wa_id, state, assigned_agent, handoff_reason, prompt_tokens, completion_tokens, updated_at)
                VALUES (?, ?, NULL, NULL, 0, 0, ?)
                """,
                (wa_id, ConversationState.BOT_ACTIVE.value, now),
            )
            self._conn.commit()
            return ConversationSession(wa_id=wa_id, state=ConversationState.BOT_ACTIVE, updated_at=now)

    def claim_message_id(self, message_id: str, wa_id: str) -> bool:
        """Atomically record a Meta wamid. Returns True if new, False if duplicate."""
        with self._lock:
            now = _utc_now_iso()
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages (message_id, wa_id, processed_at)
                VALUES (?, ?, ?)
                """,
                (message_id, wa_id, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def save_session(self, session: ConversationSession) -> None:
        with self._lock:
            session.updated_at = _utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO sessions (wa_id, state, assigned_agent, handoff_reason, prompt_tokens, completion_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wa_id) DO UPDATE SET
                    state = excluded.state,
                    assigned_agent = excluded.assigned_agent,
                    handoff_reason = excluded.handoff_reason,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    session.wa_id,
                    session.state.value,
                    session.assigned_agent,
                    session.handoff_reason,
                    session.prompt_tokens_used,
                    session.completion_tokens_used,
                    session.updated_at,
                ),
            )
            self._conn.commit()

    def append_audit(self, wa_id: str, state: ConversationState, event: str, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_events (wa_id, state, event, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (wa_id, state.value, event, detail, _utc_now_iso()),
            )
            self._conn.commit()

    def get_audit_log(self, wa_id: str, limit: int = 50) -> List[AuditEventOut]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT created_at, state, event, detail
                FROM audit_events
                WHERE wa_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (wa_id, limit),
            ).fetchall()
            return [
                AuditEventOut(
                    ts=row["created_at"],
                    state=row["state"],
                    event=row["event"],
                    detail=row["detail"],
                )
                for row in rows
            ]
