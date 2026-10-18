from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from app.utils import iso_utc
from app.backup import backup_database


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY,
    author_user_id INTEGER NOT NULL,
    content_hmac TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('publishing', 'published', 'deleted')),
    published_at_utc TEXT,
    deleted_at_utc TEXT,
    delete_failed_at_utc TEXT,
    delete_error_code TEXT,
    created_at_utc TEXT,
    submission_nonce TEXT,
    send_started_at_utc TEXT,
    publish_error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_author_time
    ON articles(author_user_id, published_at_utc);
CREATE INDEX IF NOT EXISTS idx_articles_duplicate
    ON articles(author_user_id, content_hmac, published_at_utc);

CREATE TABLE IF NOT EXISTS article_messages (
    article_id TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (article_id, sequence_no),
    UNIQUE (channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS pending_submissions (
    user_id INTEGER PRIMARY KEY,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    confirmation_message_id INTEGER,
    raw_text TEXT NOT NULL,
    callback_nonce TEXT NOT NULL UNIQUE,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_states (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked_users (
    user_id INTEGER PRIMARY KEY,
    blocked_at_utc TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    reporter_user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    UNIQUE(article_id, reporter_user_id)
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at_utc);
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_user_id, created_at_utc);
"""


class Database:
    """Small async-compatible SQLite facade.

    SQLite calls are short and synchronous here, which keeps the first VPS
    deployment dependency-light. The bot's per-user locks prevent long writes
    from competing with the same user's callback flow.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.connection: sqlite3.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(articles)")}
        if "send_started_at_utc" not in columns:
            backup_database(self.path, self.path.parent / "backups")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for name in ("created_at_utc", "submission_nonce", "send_started_at_utc", "publish_error_code"):
                if name not in columns:
                    self.connection.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT")
            if "send_started_at_utc" not in columns:
                self.connection.execute(
                    "UPDATE articles SET publish_error_code = 'interrupted_legacy_publish' "
                    "WHERE status = 'publishing'"
                )
            self.connection.execute(
                "UPDATE articles SET created_at_utc = COALESCE(published_at_utc, ?) "
                "WHERE created_at_utc IS NULL", (iso_utc(),)
            )
            self.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_submission ON articles(submission_nonce)"
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        if os.name != "nt":
            for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                if path.exists():
                    path.chmod(0o600)

    async def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def discard_incomplete_articles(self) -> int:
        """Discard only unsent work; retain interrupted sends for manual review."""
        cursor = self._conn().execute(
            """
            DELETE FROM articles
            WHERE status = 'publishing'
              AND send_started_at_utc IS NULL AND publish_error_code IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM article_messages
                  WHERE article_messages.article_id = articles.article_id
              )
            """
        )
        removed = cursor.rowcount
        self._conn().execute(
            "UPDATE articles SET publish_error_code = COALESCE(publish_error_code, 'interrupted_publish') "
            "WHERE status = 'publishing'"
        )
        return removed

    async def pending_before(self, cutoff_utc: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM pending_submissions WHERE created_at_utc < ?",
            (cutoff_utc,),
        ).fetchall()
        return [dict(row) for row in rows]

    async def purge_states_before(self, cutoff_utc: str) -> int:
        cursor = self._conn().execute(
            "DELETE FROM user_states WHERE updated_at_utc < ?",
            (cutoff_utc,),
        )
        return cursor.rowcount

    def _conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("資料庫尚未開啟")
        return self.connection

    async def replace_pending(
        self,
        *,
        user_id: int,
        source_chat_id: int,
        source_message_id: int,
        raw_text: str,
        callback_nonce: str,
        created_at_utc: str,
    ) -> dict[str, Any] | None:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            old = conn.execute(
                "SELECT * FROM pending_submissions WHERE user_id = ?", (user_id,)
            ).fetchone()
            conn.execute(
                "DELETE FROM pending_submissions WHERE user_id = ?", (user_id,)
            )
            conn.execute(
                """
                INSERT INTO pending_submissions
                (user_id, source_chat_id, source_message_id, raw_text,
                 callback_nonce, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_chat_id,
                    source_message_id,
                    raw_text,
                    callback_nonce,
                    created_at_utc,
                ),
            )
            conn.execute("COMMIT")
            return dict(old) if old else None
        except Exception:
            conn.execute("ROLLBACK")
            raise

    async def update_pending_confirmation(self, user_id: int, message_id: int) -> None:
        self._conn().execute(
            """
            UPDATE pending_submissions
            SET confirmation_message_id = ?
            WHERE user_id = ?
            """,
            (message_id, user_id),
        )

    async def get_pending_for_user(self, user_id: int) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM pending_submissions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    async def get_pending_by_nonce(self, nonce: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM pending_submissions WHERE callback_nonce = ?", (nonce,)
        ).fetchone()
        return dict(row) if row else None

    async def delete_pending(self, user_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM pending_submissions WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.execute("DELETE FROM pending_submissions WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    async def set_state(
        self,
        user_id: int,
        state: str,
        payload: dict[str, Any],
        updated_at_utc: str,
    ) -> None:
        self._conn().execute(
            """
            INSERT INTO user_states(user_id, state, payload_json, updated_at_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state,
                payload_json = excluded.payload_json,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                user_id,
                state,
                json.dumps(payload, ensure_ascii=False),
                updated_at_utc,
            ),
        )

    async def get_state(self, user_id: int) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM user_states WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "state": row["state"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "updated_at_utc": row["updated_at_utc"],
        }

    async def clear_state(self, user_id: int) -> None:
        self._conn().execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))

    async def is_blocked(self, user_id: int) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None

    async def block_user(
        self,
        user_id: int,
        blocked_at_utc: str,
        reason: str | None = None,
    ) -> None:
        self._conn().execute(
            """
            INSERT INTO blocked_users(user_id, blocked_at_utc, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                blocked_at_utc = excluded.blocked_at_utc,
                reason = excluded.reason
            """,
            (user_id, blocked_at_utc, reason),
        )

    async def unblock_user(self, user_id: int) -> None:
        self._conn().execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))

    async def list_blocked_users(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT * FROM blocked_users
            ORDER BY blocked_at_utc DESC, user_id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    async def create_article(
        self,
        article_id: str,
        author_user_id: int,
        content_hmac: str,
        submission_nonce: str | None = None,
    ) -> None:
        self._conn().execute(
            """
            INSERT INTO articles(article_id, author_user_id, content_hmac, status, created_at_utc, submission_nonce)
            VALUES (?, ?, ?, 'publishing', ?, ?)
            """,
            (article_id, author_user_id, content_hmac, iso_utc(), submission_nonce),
        )

    async def delete_article(self, article_id: str) -> None:
        self._conn().execute("DELETE FROM articles WHERE article_id = ?", (article_id,))

    async def mark_published(self, article_id: str, published_at_utc: str) -> None:
        self._conn().execute(
            """
            UPDATE articles
            SET status = 'published', published_at_utc = ?, publish_error_code = NULL
            WHERE article_id = ?
            """,
            (published_at_utc, article_id),
        )

    async def save_article_messages(
        self,
        article_id: str,
        channel_id: int,
        message_ids: list[int],
    ) -> None:
        if len(set(message_ids)) != len(message_ids):
            raise sqlite3.IntegrityError("同一篇文章不可重複保存 Telegram message ID")

        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sequence_no, message_id in enumerate(message_ids, 1):
                by_sequence = conn.execute(
                    """
                    SELECT channel_id, message_id
                    FROM article_messages
                    WHERE article_id = ? AND sequence_no = ?
                    """,
                    (article_id, sequence_no),
                ).fetchone()
                if by_sequence:
                    if (
                        int(by_sequence["channel_id"]) != channel_id
                        or int(by_sequence["message_id"]) != message_id
                    ):
                        raise sqlite3.IntegrityError(
                            "文章 sequence_no 已經對應到不同的 Telegram message ID"
                        )
                    # The publish flow may retry after the Telegram request
                    # succeeded but the following database update failed.
                    continue

                by_message = conn.execute(
                    """
                    SELECT article_id, sequence_no
                    FROM article_messages
                    WHERE channel_id = ? AND message_id = ?
                    """,
                    (channel_id, message_id),
                ).fetchone()
                if by_message:
                    raise sqlite3.IntegrityError(
                        "Telegram message ID 已經屬於其他文章"
                    )

                conn.execute(
                    """
                    INSERT INTO article_messages
                        (article_id, sequence_no, channel_id, message_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (article_id, sequence_no, channel_id, message_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    async def get_article(self, article_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM articles WHERE article_id = ?", (article_id,)
        ).fetchone()
        return dict(row) if row else None

    async def get_article_messages(self, article_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT * FROM article_messages
            WHERE article_id = ?
            ORDER BY sequence_no
            """,
            (article_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    async def mark_deleted(self, article_id: str, deleted_at_utc: str) -> None:
        self._conn().execute(
            """
            UPDATE articles
            SET status = 'deleted', deleted_at_utc = ?,
                delete_failed_at_utc = NULL, delete_error_code = NULL, publish_error_code = NULL
            WHERE article_id = ?
            """,
            (deleted_at_utc, article_id),
        )

    async def mark_delete_failed(
        self,
        article_id: str,
        failed_at_utc: str,
        error_code: str,
    ) -> None:
        self._conn().execute(
            """
            UPDATE articles
            SET delete_failed_at_utc = ?, delete_error_code = ?
            WHERE article_id = ?
            """,
            (failed_at_utc, error_code[:120], article_id),
        )

    async def has_recent_duplicate(
        self,
        user_id: int,
        content_hash: str,
        since_utc: str,
    ) -> bool:
        row = self._conn().execute(
            """
            SELECT 1 FROM articles
            WHERE author_user_id = ? AND content_hmac = ?
              AND ((status IN ('published', 'deleted') AND published_at_utc >= ?)
                   OR (status = 'publishing' AND
                       (send_started_at_utc IS NOT NULL OR publish_error_code IS NOT NULL)))
            LIMIT 1
            """,
            (user_id, content_hash, since_utc),
        ).fetchone()
        return row is not None

    async def count_user_between(
        self,
        user_id: int,
        start_utc: str,
        end_utc: str,
    ) -> int:
        row = self._conn().execute(
            """
            SELECT COUNT(*) AS count FROM articles
            WHERE author_user_id = ? AND status IN ('published', 'deleted')
              AND published_at_utc >= ? AND published_at_utc < ?
            """,
            (user_id, start_utc, end_utc),
        ).fetchone()
        return int(row["count"])

    async def get_article_by_nonce(self, nonce: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM articles WHERE submission_nonce = ?", (nonce,)
        ).fetchone()
        return dict(row) if row else None

    async def mark_send_started(self, article_id: str) -> None:
        self._conn().execute(
            "UPDATE articles SET send_started_at_utc = ? WHERE article_id = ?",
            (iso_utc(), article_id),
        )

    async def clear_send_started(self, article_id: str) -> None:
        self._conn().execute(
            "UPDATE articles SET send_started_at_utc = NULL WHERE article_id = ?", (article_id,)
        )

    async def mark_publish_uncertain(self, article_id: str, error_code: str) -> None:
        self._conn().execute(
            "UPDATE articles SET publish_error_code = ? WHERE article_id = ?",
            (error_code, article_id),
        )

    async def submissions_paused(self) -> bool:
        row = self._conn().execute(
            "SELECT value FROM bot_settings WHERE key = 'submissions_paused'"
        ).fetchone()
        return bool(row and row["value"] == "1")

    async def set_submissions_paused(self, paused: bool) -> None:
        self._conn().execute(
            "INSERT INTO bot_settings(key, value) VALUES ('submissions_paused', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("1" if paused else "0",)
        )

    async def list_user_articles(self, user_id: int, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT a.*, m.channel_id, m.message_id FROM articles a "
            "LEFT JOIN article_messages m ON m.article_id = a.article_id AND m.sequence_no = 1 "
            "WHERE a.author_user_id = ? "
            "ORDER BY COALESCE(a.published_at_utc, a.created_at_utc) DESC, a.article_id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    async def list_attention_articles(self, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM articles WHERE publish_error_code IS NOT NULL OR delete_error_code IS NOT NULL "
            "ORDER BY COALESCE(delete_failed_at_utc, send_started_at_utc, created_at_utc) DESC, article_id DESC "
            "LIMIT ? OFFSET ?", (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    async def create_report(self, article_id: str, user_id: int, reason: str, now: str, since: str) -> str:
        if not reason.strip() or len(reason) > 500 or user_id <= 0:
            raise ValueError("檢舉原因需為 1 至 500 字。")
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute(
                "SELECT 1 FROM reports WHERE article_id = ? AND reporter_user_id = ?", (article_id, user_id)
            ).fetchone():
                result = "duplicate"
            elif conn.execute(
                "SELECT COUNT(*) FROM reports WHERE reporter_user_id = ? AND created_at_utc >= ?", (user_id, since)
            ).fetchone()[0] >= 3:
                result = "limited"
            else:
                conn.execute(
                    "INSERT INTO reports(article_id, reporter_user_id, reason, created_at_utc) VALUES (?, ?, ?, ?)",
                    (article_id, user_id, reason.strip(), now),
                )
                result = "created"
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise

    async def list_open_reports(self, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM reports WHERE status = 'open' ORDER BY report_id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(row) for row in rows]

    async def close_reports(self, article_id: str) -> None:
        self._conn().execute("UPDATE reports SET status = 'closed' WHERE article_id = ?", (article_id,))

    async def oldest_user_publication_between(
        self,
        user_id: int,
        start_utc: str,
        end_utc: str,
    ) -> str | None:
        row = self._conn().execute(
            """
            SELECT published_at_utc FROM articles
            WHERE author_user_id = ? AND status IN ('published', 'deleted')
              AND published_at_utc >= ? AND published_at_utc < ?
            ORDER BY published_at_utc ASC
            LIMIT 1
            """,
            (user_id, start_utc, end_utc),
        ).fetchone()
        return str(row["published_at_utc"]) if row else None

    async def count_published_between(self, start_utc: str, end_utc: str) -> int:
        row = self._conn().execute(
            """
            SELECT COUNT(*) AS count FROM articles
            WHERE status IN ('published', 'deleted')
              AND published_at_utc >= ? AND published_at_utc < ?
            """,
            (start_utc, end_utc),
        ).fetchone()
        return int(row["count"])
