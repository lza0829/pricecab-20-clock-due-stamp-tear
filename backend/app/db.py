from __future__ import annotations

import sqlite3
from typing import Any

from app.config import DB_PATH

DEFAULT_TIMEOUT_SECONDS = 5.0


def connect(*, immediate: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(max(timeout, 0) * 1000)}")
    # WAL：应用持写事务期间，拨钟仍能读到锁行以判定冲突（读不被写阻塞）。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None
