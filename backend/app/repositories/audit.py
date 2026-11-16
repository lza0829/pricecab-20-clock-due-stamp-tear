from __future__ import annotations

import sqlite3
import threading

from app.config import AUDIT_DB_PATH

# 冲突 / 中止回执放独立审计库：应用持锁推进时持有主库写事务，
# 拨钟的「被拒」回执若写主库会被阻塞；独立库保证回执不丢。
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS apply_conflicts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  policy TEXT NOT NULL,
  winner TEXT NOT NULL,
  loser TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS apply_batch_aborts (
  id INTEGER PRIMARY KEY,
  holder TEXT NOT NULL,
  fence INTEGER NOT NULL,
  reason TEXT NOT NULL,
  policy TEXT NOT NULL,
  written_lines INTEGER NOT NULL DEFAULT 0,
  rolled_back INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
'''

_init_lock = threading.Lock()
_initialized = False


def connect(*, immediate: bool = False, timeout: float = 0.5) -> sqlite3.Connection:
    conn = sqlite3.connect(AUDIT_DB_PATH, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(max(timeout, 0) * 1000)}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = connect(immediate=True, timeout=2.0)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def record_conflict(
    *, kind: str, policy: str, winner: str, loser: str, detail: str, created_at: str
) -> None:
    try:
        ensure_schema()
        conn = connect()
        try:
            conn.execute(
                '''
                INSERT INTO apply_conflicts(kind, policy, winner, loser, detail, created_at)
                VALUES (?,?,?,?,?,?)
                ''',
                (kind, policy, winner, loser, detail, created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        # 回执属于旁路审计，写入失败不得改变主流程的判定。
        pass


def record_abort(
    *,
    holder: str,
    fence: int,
    reason: str,
    policy: str,
    written_lines: int,
    created_at: str,
) -> None:
    try:
        ensure_schema()
        conn = connect()
        try:
            conn.execute(
                '''
                INSERT INTO apply_batch_aborts(holder, fence, reason, policy,
                                               written_lines, rolled_back, created_at)
                VALUES (?,?,?,?,?,1,?)
                ''',
                (holder, fence, reason, policy, written_lines, created_at),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def list_conflicts(limit: int = 20) -> list[dict]:
    ensure_schema()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM apply_conflicts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_aborts(limit: int = 20) -> list[dict]:
    ensure_schema()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM apply_batch_aborts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear() -> None:
    """仅测试/重置用。"""
    ensure_schema()
    conn = connect()
    try:
        conn.execute("DELETE FROM apply_conflicts")
        conn.execute("DELETE FROM apply_batch_aborts")
        conn.commit()
    finally:
        conn.close()
