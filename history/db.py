from __future__ import annotations

import logging
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

log = logging.getLogger("LiveTranslate.History")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryStore:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._subtitle_ids: dict[int, str] = {}
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            self.start_session()
        return self._session_id

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS config (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS translation_history (
                  id TEXT PRIMARY KEY,
                  source_type TEXT NOT NULL,
                  original_text TEXT NOT NULL,
                  translated_text TEXT NOT NULL,
                  explanation TEXT,
                  polished_text TEXT,
                  source_language TEXT,
                  target_language TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  provider TEXT,
                  model TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subtitle_history (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  original_text TEXT,
                  translated_text TEXT,
                  source_language TEXT,
                  target_language TEXT,
                  start_time REAL,
                  end_time REAL,
                  provider TEXT,
                  model TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY,
                  title TEXT,
                  source_type TEXT,
                  started_at TEXT NOT NULL,
                  ended_at TEXT
                );
                """
            )
            conn.commit()

    def start_session(self, title: str | None = None, source_type: str = "subtitle") -> str:
        with self._lock, self._connect() as conn:
            self._start_session_locked(conn, title=title, source_type=source_type)
            conn.commit()
            log.info("History session started: %s", self._session_id)
            return self._session_id

    def _start_session_locked(
        self,
        conn: sqlite3.Connection,
        title: str | None = None,
        source_type: str = "subtitle",
    ):
        self._session_id = uuid4().hex
        conn.execute(
            """
            INSERT INTO sessions (id, title, source_type, started_at, ended_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (
                self._session_id,
                title or datetime.now().strftime("LiveTranslate %Y-%m-%d %H:%M:%S"),
                source_type,
                utc_now(),
            ),
        )
        self._subtitle_ids.clear()

    def end_session(self):
        if self._session_id is None:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (utc_now(), self._session_id),
            )
            conn.commit()
            log.info("History session ended: %s", self._session_id)
            self._session_id = None
            self._subtitle_ids.clear()

    def set_config(self, key: str, value):
        if not key:
            raise ValueError("config key is required")
        serialized = json.dumps(value, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO config (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (key, serialized, utc_now()),
            )
            conn.commit()

    def get_config(self, key: str, default=None):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def add_subtitle_original(
        self,
        msg_id: int,
        original_text: str,
        source_language: str | None,
        target_language: str | None,
        start_time: float | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        record_id = uuid4().hex
        with self._lock, self._connect() as conn:
            if self._session_id is None:
                self._start_session_locked(conn)
            conn.execute(
                """
                INSERT INTO subtitle_history (
                    id, session_id, original_text, translated_text, source_language,
                    target_language, start_time, end_time, provider, model, created_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    record_id,
                    self._session_id,
                    original_text,
                    source_language,
                    target_language,
                    start_time,
                    provider,
                    model,
                    utc_now(),
                ),
            )
            conn.commit()
            self._subtitle_ids[msg_id] = record_id
            return record_id

    def update_subtitle_translation(
        self,
        msg_id: int,
        translated_text: str | None,
        end_time: float | None = None,
        provider: str | None = None,
        model: str | None = None,
    ):
        record_id = self._subtitle_ids.pop(msg_id, None)
        if record_id is None:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE subtitle_history
                SET translated_text = COALESCE(?, translated_text),
                    end_time = COALESCE(?, end_time),
                    provider = COALESCE(?, provider),
                    model = COALESCE(?, model)
                WHERE id = ?
                """,
                (translated_text, end_time, provider, model, record_id),
            )
            conn.commit()

    def update_subtitle_original(
        self,
        msg_id: int,
        original_text: str,
        source_language: str | None = None,
    ):
        record_id = self._subtitle_ids.get(msg_id)
        if record_id is None:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE subtitle_history
                SET original_text = ?,
                    source_language = COALESCE(?, source_language)
                WHERE id = ?
                """,
                (original_text, source_language, record_id),
            )
            conn.commit()

    def add_translation_history(
        self,
        source_type: str,
        original_text: str,
        translated_text: str,
        target_language: str,
        mode: str,
        explanation: str | None = None,
        polished_text: str | None = None,
        source_language: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        record_id = uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO translation_history (
                    id, source_type, original_text, translated_text, explanation,
                    polished_text, source_language, target_language, mode,
                    provider, model, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    source_type,
                    original_text,
                    translated_text,
                    explanation,
                    polished_text,
                    source_language,
                    target_language,
                    mode,
                    provider,
                    model,
                    utc_now(),
                ),
            )
            conn.commit()
            return record_id

    def list_history(self, source_filter: str = "all", limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        rows: list[dict] = []
        with self._lock, self._connect() as conn:
            if source_filter in ("all", "subtitle"):
                for row in conn.execute(
                    """
                    SELECT id, 'subtitle_history' AS table_name, 'subtitle' AS source_type,
                           original_text, translated_text, NULL AS explanation,
                           NULL AS polished_text, source_language, target_language,
                           'natural' AS mode, provider, model, created_at,
                           session_id, start_time, end_time
                    FROM subtitle_history
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ):
                    rows.append(dict(row))

            include_translation = source_filter == "all" or source_filter not in ("subtitle",)
            if include_translation:
                params: tuple
                where = ""
                if source_filter != "all":
                    if source_filter == "selection":
                        where = (
                            "WHERE source_type IN "
                            "('selection', 'global-hotkey', 'chrome-extension', 'local-api')"
                        )
                        params = (limit,)
                    else:
                        where = "WHERE source_type = ?"
                        params = (source_filter, limit)
                else:
                    params = (limit,)
                for row in conn.execute(
                    f"""
                    SELECT id, 'translation_history' AS table_name, source_type,
                           original_text, translated_text, explanation,
                           polished_text, source_language, target_language,
                           mode, provider, model, created_at,
                           NULL AS session_id, NULL AS start_time, NULL AS end_time
                    FROM translation_history
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    params,
                ):
                    rows.append(dict(row))

        rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return rows[:limit]

    def get_session_subtitles(self, session_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, original_text, translated_text,
                       source_language, target_language, start_time, end_time,
                       provider, model, created_at
                FROM subtitle_history
                WHERE session_id = ?
                ORDER BY COALESCE(start_time, 0), created_at
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_history(self, table: str, record_id: str):
        if table not in {"translation_history", "subtitle_history", "sessions"}:
            raise ValueError(f"Unsupported history table: {table}")
        with self._lock, self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            conn.commit()

    def clear_history(self, source_filter: str = "all"):
        with self._lock, self._connect() as conn:
            if source_filter in ("all", "subtitle"):
                conn.execute("DELETE FROM subtitle_history")
            if source_filter == "all":
                conn.execute("DELETE FROM translation_history")
                conn.execute("DELETE FROM sessions")
            elif source_filter != "subtitle":
                if source_filter == "selection":
                    conn.execute(
                        """
                        DELETE FROM translation_history
                        WHERE source_type IN
                          ('selection', 'global-hotkey', 'chrome-extension', 'local-api')
                        """
                    )
                else:
                    conn.execute(
                        "DELETE FROM translation_history WHERE source_type = ?",
                        (source_filter,),
                    )
            conn.commit()
