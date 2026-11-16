from __future__ import annotations

import sqlite3
import threading

from app.config import SIGNAL_DB_PATH

# 「拨钟优先」的中止信号。
#
# 为什么单独一个库：
#  - 应用持锁推进时握着主库写事务，拨钟若写主库会被阻塞，无法及时登记冲突；
#  - 信号写入与应用提交必须互斥。应用在「提交前检查 → commit」期间持有本库
#    写事务，拨钟只能在应用提交之后才写得进信号，于是不会出现「检查通过、
#    commit 之后信号才到」导致的半吊子状态。
#
# 单行表 id 恒为 1；token 为空表示无信号。
SIGNAL_ID = 1

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS apply_preempt (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  token TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL DEFAULT '',
  requested_epoch REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO apply_preempt(id) VALUES (1);
'''

_init_lock = threading.Lock()
_initialized = False


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


def connect(*, immediate: bool = False, timeout: float = 2.0) -> sqlite3.Connection:
    conn = sqlite3.connect(SIGNAL_DB_PATH, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(max(timeout, 0) * 1000)}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def write_preempt(
    conn: sqlite3.Connection, *, token: str, source: str, requested_at: str, requested_epoch: float
) -> None:
    """在调用方已开启的写事务内登记中止信号。"""
    conn.execute(
        '''
        INSERT INTO apply_preempt(id, token, source, requested_at, requested_epoch)
        VALUES (?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          token=excluded.token,
          source=excluded.source,
          requested_at=excluded.requested_at,
          requested_epoch=excluded.requested_epoch
        ''',
        (SIGNAL_ID, token, source, requested_at, requested_epoch),
    )


def read_preempt(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM apply_preempt WHERE id=?", (SIGNAL_ID,)).fetchone()
    if not row or not row["token"]:
        return None
    return dict(row)


def clear_preempt(conn: sqlite3.Connection, token: str) -> None:
    conn.execute(
        "UPDATE apply_preempt SET token='', source='', requested_at='', requested_epoch=0 "
        "WHERE id=? AND token=?",
        (SIGNAL_ID, token),
    )


def read_preempt_standalone() -> dict | None:
    """只读窥探，用于应用在写入阶段之间（不持有信号库事务时）检查。"""
    ensure_schema()
    conn = connect()
    try:
        return read_preempt(conn)
    finally:
        conn.close()
