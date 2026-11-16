from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

# 生效临界区锁与业务数据同在 pricecab.db。
# 这样「读到期集合 → 写价签/历史 → 更新单状态 → 落批次」与锁的复核/释放在
# 同一 SQLite 写者序列中完成，天然同成同败；锁被超时回收后，原持有者在提交
# 前的 fence 复核必然失败，只能整批回滚，不可能与回收方交错写入。
#
# 单行表 id 恒为 1；holder 为空表示空闲；fence 每次获取/回收单调 +1。
LOCK_ID = 1


class LockHeld(Exception):
    def __init__(self, row: dict[str, Any]):
        self.row = row
        super().__init__(f"临界区锁被 {row.get('holder') or '?'} 持有")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_lock(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM apply_lock WHERE id=?", (LOCK_ID,)).fetchone()
    if not row or not row["holder"]:
        return None
    return dict(row)


def is_free(row: dict[str, Any] | None) -> bool:
    return not row or not row.get("holder")


def is_expired(row: dict[str, Any] | None, at: datetime | None = None) -> bool:
    """被占用且超过 expires_at 才算超时；空闲锁不算超时。"""
    if is_free(row):
        return False
    return float(row.get("expires_epoch") or 0) <= (at or now_utc()).timestamp()


def current_fence(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT fence FROM apply_lock WHERE id=?", (LOCK_ID,)).fetchone()
    return int(row["fence"]) if row else 0


def acquire(
    conn: sqlite3.Connection,
    *,
    holder: str,
    kind: str,
    ttl_seconds: float,
    at: datetime | None = None,
) -> int:
    """在调用方已开启的写事务内获取锁；被占用且未超时抛 LockHeld。

    判断与写入处于同一个 BEGIN IMMEDIATE 写事务，不会与另一持锁方交错。
    """
    current = get_lock(conn)
    if current and not is_expired(current, at):
        raise LockHeld(current)

    now = at or now_utc()
    expires = now + timedelta(seconds=max(ttl_seconds, 0.0))
    fence = current_fence(conn) + 1
    conn.execute(
        '''
        INSERT INTO apply_lock(id, holder, holder_kind, acquired_at, expires_at,
                               acquired_epoch, expires_epoch, fence, released_at)
        VALUES (?,?,?,?,?,?,?,?,NULL)
        ON CONFLICT(id) DO UPDATE SET
          holder=excluded.holder,
          holder_kind=excluded.holder_kind,
          acquired_at=excluded.acquired_at,
          expires_at=excluded.expires_at,
          acquired_epoch=excluded.acquired_epoch,
          expires_epoch=excluded.expires_epoch,
          fence=excluded.fence,
          released_at=NULL
        ''',
        (
            LOCK_ID,
            holder,
            kind,
            iso(now),
            iso(expires),
            now.timestamp(),
            expires.timestamp(),
            fence,
        ),
    )
    return fence


def holds(conn: sqlite3.Connection, holder: str, fence: int, at: datetime | None = None) -> bool:
    """复核锁仍归调用方：holder + fence 一致且未超时。须在同一写事务内调用。"""
    row = get_lock(conn)
    return bool(
        row
        and row.get("holder") == holder
        and int(row.get("fence") or -1) == fence
        and not is_expired(row, at)
    )


def release(conn: sqlite3.Connection, holder: str, fence: int) -> bool:
    """只释放仍属自己的锁；已被回收（fence 已变）时不触碰新持有者。"""
    cur = conn.execute(
        '''
        UPDATE apply_lock
        SET holder='', holder_kind='', acquired_at='', expires_at='',
            acquired_epoch=0, expires_epoch=0, released_at=?
        WHERE id=? AND holder=? AND fence=?
        ''',
        (iso(now_utc()), LOCK_ID, holder, fence),
    )
    return cur.rowcount > 0


def recover_stale(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """回收一把已超时的锁（仅超时才允许）。返回被回收的锁记录。

    只清锁，不触碰任何业务数据：已提交的批次保持不变，未提交的写入在
    原事务里随进程/连接结束而消失，因此不存在「改写他人已成功批次」。
    """
    row = get_lock(conn)
    if not row or not is_expired(row):
        return None
    fence = current_fence(conn) + 1
    conn.execute(
        '''
        UPDATE apply_lock
        SET holder='', holder_kind='', acquired_at='', expires_at='',
            acquired_epoch=0, expires_epoch=0, fence=?, released_at=?
        WHERE id=?
        ''',
        (fence, iso(now_utc()), LOCK_ID),
    )
    return row


def describe(conn: sqlite3.Connection) -> dict[str, Any]:
    row = get_lock(conn)
    if not row:
        return {"held": False, "stale": False, "lock": None, "last_fence": current_fence(conn)}
    remaining = float(row.get("expires_epoch") or 0) - now_utc().timestamp()
    return {
        "held": remaining > 0,
        "stale": remaining <= 0,
        "lock": {
            "holder": row["holder"],
            "holder_kind": row["holder_kind"],
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "fence": int(row["fence"]),
            "remaining_seconds": round(max(remaining, 0.0), 3),
        },
        "last_fence": int(row["fence"]),
    }
