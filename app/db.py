#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存取层。

为什么是 SQLite：这是**单进程 + 单机**的网关（协调器必须单实例，否则同一任务
会被提交两次 = 双倍计费）；SQLite 的 WAL 模式在这个形态下完全够用，而且
"备份 = 复制一个文件"。多副本需求出现时再把 `db.py` 换成 PostgreSQL，
不要为了想象中的规模先付部署复杂度。

锁策略：一个进程内 `threading.RLock`。所有写都在毫秒级，且不跨网络调用；
真瓶颈在上游，不在库。**不要**把上游请求放进事务里。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import settings

_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT    NOT NULL DEFAULT '',
    name          TEXT    NOT NULL DEFAULT '',
    sessionid     TEXT    NOT NULL,
    remark        TEXT    NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'unknown',
    credits       INTEGER,
    credit_detail TEXT    NOT NULL DEFAULT '{}',
    member_level  TEXT    NOT NULL DEFAULT '',
    expires_at    INTEGER,
    cooldown_until INTEGER,
    last_error    TEXT    NOT NULL DEFAULT '',
    last_checked  INTEGER,
    last_used_at  INTEGER,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    inflight      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_session ON accounts(sessionid);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status, enabled);

CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    key_hash      TEXT    NOT NULL UNIQUE,
    key_prefix    TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    quota_credits INTEGER NOT NULL DEFAULT 0,
    used_credits  INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    last_used_at  INTEGER,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    api_key_id    INTEGER,
    account_id    INTEGER,
    mode          TEXT NOT NULL DEFAULT 't2i',
    model         TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    size          TEXT NOT NULL DEFAULT '2048x2048',
    width         INTEGER NOT NULL DEFAULT 2048,
    height        INTEGER NOT NULL DEFAULT 2048,
    resolution    TEXT NOT NULL DEFAULT '2k',
    count         INTEGER NOT NULL DEFAULT 1,
    seed          INTEGER,
    image_uris    TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'queued',
    upstream_status INTEGER,
    progress      TEXT NOT NULL DEFAULT '',
    images        TEXT NOT NULL DEFAULT '[]',
    error         TEXT NOT NULL DEFAULT '',
    error_kind    TEXT NOT NULL DEFAULT '',
    forecast_credits INTEGER,
    charged_credits  INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    submit_draft  TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER,
    updated_at    INTEGER NOT NULL,
    next_poll_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, next_poll_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account_id, status);

CREATE TABLE IF NOT EXISTS request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    api_key_id  INTEGER,
    task_id     TEXT,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    status      INTEGER NOT NULL,
    latency_ms  INTEGER NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    ua          TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reqlog_ts ON request_logs(ts DESC);

CREATE TABLE IF NOT EXISTS upstream_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    account_id  INTEGER,
    task_id     TEXT,
    path        TEXT NOT NULL,
    http_status INTEGER,
    ret         TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    excerpt     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_uplog_ts ON upstream_logs(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(
            settings.db_path, check_same_thread=False, timeout=15,
        )
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def init() -> None:
    with _LOCK:
        conn()
        # 已有库也要收紧权限（首次创建时靠 umask，历史库靠这里）
        try:
            os.chmod(settings.db_path, 0o600)
        except OSError:
            pass


@contextmanager
def tx():
    with _LOCK:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _LOCK:
        return list(conn().execute(sql, tuple(args)).fetchall())


def one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    with _LOCK:
        return conn().execute(sql, tuple(args)).fetchone()


def scalar(sql: str, args: Iterable[Any] = (), default: Any = None) -> Any:
    row = one(sql, args)
    if row is None:
        return default
    v = row[0]
    return default if v is None else v


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    with tx() as c:
        cur = c.execute(sql, tuple(args))
        return cur.lastrowid or 0


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with tx() as c:
        c.executemany(sql, [tuple(x) for x in seq])


def now() -> int:
    return int(time.time())


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    return loads(row["value"], default)


def put_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, dumps(value)),
    )


def log_upstream(*, account_id: int | None, task_id: str | None, path: str,
                 http_status: int | None, ret: str, kind: str,
                 latency_ms: int, error: str = "", excerpt: str = "") -> None:
    execute(
        "INSERT INTO upstream_logs(ts, account_id, task_id, path, http_status, "
        "ret, kind, latency_ms, error, excerpt) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), account_id, task_id, path, http_status, ret, kind,
         latency_ms, error[:500], excerpt[:800]),
    )


def log_request(*, api_key_id: int | None, task_id: str | None, method: str,
                path: str, status: int, latency_ms: int, ip: str = "",
                ua: str = "", detail: str = "") -> None:
    execute(
        "INSERT INTO request_logs(ts, api_key_id, task_id, method, path, status, "
        "latency_ms, ip, ua, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), api_key_id, task_id, method, path, status, latency_ms,
         ip[:64], ua[:200], detail[:500]),
    )
