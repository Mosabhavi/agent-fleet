"""SQLite-backed state: transcript, tasks, schedule, audit log, poll offset."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chat_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    agent_key TEXT,
    is_owner INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    tg_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    owner_agent TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    resolved REAL
);
CREATE TABLE IF NOT EXISTS schedule (
    agent_key TEXT PRIMARY KEY,
    cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run REAL,
    last_status TEXT
);
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent_key TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notes (
    agent_key TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (agent_key, key)
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- key/value -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # ---- transcript ------------------------------------------------
    def add_message(self, chat_id: str, speaker: str, text: str, *,
                    agent_key: str | None = None, is_owner: bool = False,
                    tg_message_id: int | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO messages(ts,chat_id,speaker,agent_key,is_owner,text,tg_message_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), str(chat_id), speaker, agent_key, int(is_owner), text, tg_message_id),
            )

    def transcript(self, chat_id: str, limit: int = 40) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (str(chat_id), limit),
        ).fetchall()
        return list(reversed(rows))

    def transcript_since(self, chat_id: str, seconds: float) -> list[sqlite3.Row]:
        cutoff = time.time() - seconds
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND ts>=? ORDER BY id",
            (str(chat_id), cutoff),
        ).fetchall()
        return list(rows)

    # ---- tasks -----------------------------------------------------
    def add_task(self, owner_agent: str, title: str, detail: str = "",
                 status: str = "proposed") -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO tasks(created,owner_agent,title,detail,status) VALUES(?,?,?,?,?)",
                (time.time(), owner_agent, title, detail, status),
            )
        return int(cur.lastrowid)

    def list_tasks(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            q = "SELECT * FROM tasks WHERE status=? ORDER BY id"
            return list(self._conn.execute(q, (status,)).fetchall())
        q = "SELECT * FROM tasks WHERE status!='rejected' ORDER BY id"
        return list(self._conn.execute(q).fetchall())

    def set_task_status(self, task_id: int, status: str) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status=?, resolved=? WHERE id=?",
                (status, time.time() if status in ("done", "rejected") else None, task_id),
            )
        return cur.rowcount > 0

    # ---- schedule --------------------------------------------------
    def seed_schedule(self, agent_key: str, cron: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO schedule(agent_key,cron) VALUES(?,?) "
                "ON CONFLICT(agent_key) DO NOTHING",
                (agent_key, cron),
            )

    def schedule_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM schedule ORDER BY agent_key").fetchall())

    def schedule_for(self, agent_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM schedule WHERE agent_key=?", (agent_key,)
        ).fetchone()

    def set_cron(self, agent_key: str, cron: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE schedule SET cron=? WHERE agent_key=?", (cron, agent_key))

    def set_enabled(self, agent_key: str, enabled: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE schedule SET enabled=? WHERE agent_key=?",
                      (int(enabled), agent_key))

    def mark_run(self, agent_key: str, status: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE schedule SET last_run=?, last_status=? WHERE agent_key=?",
                      (time.time(), status, agent_key))

    # ---- audit -----------------------------------------------------
    def add_audit(self, agent_key: str, verdict: str, detail: str = "") -> None:
        with self._tx() as c:
            c.execute("INSERT INTO audits(ts,agent_key,verdict,detail) VALUES(?,?,?,?)",
                      (time.time(), agent_key, verdict, detail))

    def recent_audits(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,)).fetchall())

    # ---- notes -----------------------------------------------------
    def remember(self, agent_key: str, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO notes(agent_key,key,value,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(agent_key,key) DO UPDATE SET value=excluded.value, "
                "updated=excluded.updated",
                (agent_key, key, value, time.time()),
            )

    def recall(self, agent_key: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key,value FROM notes WHERE agent_key=? ORDER BY updated DESC LIMIT 50",
            (agent_key,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def close(self) -> None:
        self._conn.close()
