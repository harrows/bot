from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS last_check (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    checked_at TEXT,
    has_slots INTEGER,
    summary TEXT
);
"""


@dataclass(frozen=True)
class LastCheck:
    checked_at: Optional[str]
    has_slots: Optional[bool]
    summary: Optional[str]


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    async def aadd_subscriber(self, chat_id: int, created_at: str) -> None:
        await asyncio.to_thread(self._add_subscriber, chat_id, created_at)

    def _add_subscriber(self, chat_id: int, created_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO subscribers(chat_id, created_at) VALUES(?, ?)",
                (chat_id, created_at),
            )

    async def aremove_subscriber(self, chat_id: int) -> None:
        await asyncio.to_thread(self._remove_subscriber, chat_id)

    def _remove_subscriber(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    async def alist_subscribers(self) -> List[int]:
        return await asyncio.to_thread(self._list_subscribers)

    def _list_subscribers(self) -> List[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM subscribers ORDER BY created_at"
            ).fetchall()
        return [int(r[0]) for r in rows]

    async def aset_setting(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set_setting, key, value)

    def _set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    async def aget_setting(self, key: str) -> Optional[str]:
        return await asyncio.to_thread(self._get_setting, key)

    def _get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    async def aget_interval_seconds(self, default_value: int) -> int:
        v = await self.aget_setting("interval_seconds")
        if not v:
            return default_value
        try:
            return max(30, int(v))
        except ValueError:
            return default_value

    async def aupdate_last_check(self, checked_at: str, has_slots: bool, summary: str) -> None:
        await asyncio.to_thread(self._update_last_check, checked_at, has_slots, summary)

    def _update_last_check(self, checked_at: str, has_slots: bool, summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO last_check(id, checked_at, has_slots, summary) VALUES(1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at, has_slots=excluded.has_slots, summary=excluded.summary",
                (checked_at, 1 if has_slots else 0, summary),
            )

    async def aget_last_check(self) -> LastCheck:
        return await asyncio.to_thread(self._get_last_check)

    def _get_last_check(self) -> LastCheck:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT checked_at, has_slots, summary FROM last_check WHERE id = 1"
            ).fetchone()
        if not row:
            return LastCheck(None, None, None)
        checked_at, has_slots, summary = row
        return LastCheck(checked_at, bool(has_slots) if has_slots is not None else None, summary)
