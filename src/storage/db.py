"""SQLite 数据库封装。

- 流水真源（只追加）：``events`` / ``tool_calls`` / ``reservations``（见架构 6.1/6.2/6.5）。
- 派生索引（可重建）：``memories`` / ``relations`` / ``wiki_chunks`` / ``wiki_fts``（见 6.3/6.4/6.6/6.7）。

连接用单例 + 线程锁保护；异步调用方应通过 ``asyncio.to_thread`` 包装。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from src import config


def now_iso() -> str:
    """当前时间，ISO8601（带本地偏移）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_status ON tool_calls(status);
CREATE INDEX IF NOT EXISTS idx_tool_calls_event ON tool_calls(event_id);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    source_event_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    updated_at TEXT NOT NULL,
    path TEXT,
    FOREIGN KEY(source_event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    source_event_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    path TEXT,
    FOREIGN KEY(source_event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject);

CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    library TEXT,
    area TEXT,
    seat TEXT,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL,
    source_event_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY(source_event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
CREATE INDEX IF NOT EXISTS idx_reservations_date ON reservations(date);

CREATE TABLE IF NOT EXISTS wiki_chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT,
    section TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_path ON wiki_chunks(path);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_doc ON wiki_chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    chunk_id,
    title,
    section,
    content,
    tokenize = 'trigram'
);

-- 身份层（多用户 + 跨渠道关联，见 README「多用户记忆」）。
-- persons 是「人」的稳定标识；一个人可拥有多个 accounts（qq / wechat / cli）。
-- 记忆按 person_id 分区到 wiki/users/<person_id>/，非 users/ 路径为全员共享知识。
CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    channel TEXT NOT NULL,        -- qq | wechat | cli
    account_id TEXT NOT NULL,     -- 平台用户号（QQ 即 QQ 号，微信即 wxid）
    person_id TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, account_id),
    FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE INDEX IF NOT EXISTS idx_accounts_person ON accounts(person_id);

-- 跨渠道绑定的一次性验证码（聊天内绑定 + 验证码，防冒认）。
CREATE TABLE IF NOT EXISTS bind_codes (
    code TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,          -- 发起方的 person
    channel TEXT NOT NULL,            -- 发起方渠道
    target_channel TEXT NOT NULL,     -- 待绑定渠道（如 qq）
    target_account_id TEXT NOT NULL,  -- 发起方声称的对方账号（如 QQ 号）
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL              -- pending | used | expired
);
CREATE INDEX IF NOT EXISTS idx_bind_codes_status ON bind_codes(status);
"""


class Database:
    def __init__(self, path: Optional[str] = None) -> None:
        config.ensure_dirs()
        self.path = str(path or config.SQLITE_PATH)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self.init_schema()

    # ---- schema -----------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- 低层执行 ---------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- events（只追加）-------------------------------------------------
    def add_event(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        event_type: str,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        event_id = new_id("evt_")
        self.execute(
            "INSERT INTO events (id, session_id, user_id, role, content, event_type, created_at, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                session_id,
                user_id,
                role,
                content,
                event_type,
                now_iso(),
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
            ),
        )
        return event_id

    def recent_events(
        self, session_id: str, limit: int = 12, event_types: Optional[Iterable[str]] = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM events WHERE session_id = ?"
        params: list[Any] = [session_id]
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            sql += f" AND event_type IN ({placeholders})"
            params.extend(event_types)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self.query(sql, params)
        return list(reversed(rows))

    # ---- tool_calls -------------------------------------------------------
    def add_tool_call(
        self,
        *,
        event_id: str,
        tool_name: str,
        arguments: dict,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> str:
        call_id = new_id("tc_")
        self.execute(
            "INSERT INTO tool_calls (id, event_id, tool_name, arguments, result, status, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                event_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False),
                result,
                status,
                error,
                now_iso(),
            ),
        )
        return call_id

    def update_tool_call(
        self,
        call_id: str,
        *,
        status: Optional[str] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if result is not None:
            sets.append("result = ?")
            params.append(result)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if not sets:
            return
        params.append(call_id)
        self.execute(f"UPDATE tool_calls SET {', '.join(sets)} WHERE id = ?", params)

    # ---- reservations -----------------------------------------------------
    def add_reservation(
        self,
        *,
        date: str,
        start_time: str,
        end_time: str,
        status: str,
        library: Optional[str] = None,
        area: Optional[str] = None,
        seat: Optional[str] = None,
        source_event_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        res_id = new_id("res_")
        self.execute(
            "INSERT INTO reservations (id, library, area, seat, date, start_time, end_time, status,"
            " source_event_id, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                res_id,
                library,
                area,
                seat,
                date,
                start_time,
                end_time,
                status,
                source_event_id,
                now_iso(),
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
            ),
        )
        return res_id

    def update_reservation_status(self, res_id: str, status: str) -> None:
        self.execute("UPDATE reservations SET status = ? WHERE id = ?", (status, res_id))

    def list_reservations(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM reservations ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: Optional[Database] = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """进程级单例数据库。"""
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database()
    return _db
