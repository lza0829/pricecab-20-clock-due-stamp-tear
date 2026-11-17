from __future__ import annotations

import sqlite3
from typing import Any


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"] + 1
    return f"AB-{n:04d}"


def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    clock_at: str,
    scan_clock: str,
    ticket_count: int,
    line_count: int,
    status: str,
    created_at: str,
) -> int:
    """落批次。clock_at（戳记钟）与 scan_clock（扫描判定钟）必须来自同一设置键读出。"""
    cur = conn.execute(
        '''
        INSERT INTO apply_batches(batch_no, clock_at, scan_clock, ticket_count, line_count,
                                  status, created_at)
        VALUES (?,?,?,?,?,?,?)
        ''',
        (batch_no, clock_at, scan_clock, ticket_count, line_count, status, created_at),
    )
    return int(cur.lastrowid)


def list_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT b.*,
               (SELECT COUNT(*) FROM price_tickets t WHERE t.apply_batch_id = b.id) AS linked_ticket_count
        FROM apply_batches b
        ORDER BY b.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM apply_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def batches_linked_to_ticket(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT t.ticket_no, t.status AS ticket_status, t.effective_at, t.applied_at,
               t.apply_batch_id,
               b.batch_no, b.clock_at, b.scan_clock, b.status AS batch_status, b.voided_at
        FROM price_tickets t
        LEFT JOIN apply_batches b ON b.id = t.apply_batch_id
        WHERE t.apply_batch_id IS NOT NULL
        ORDER BY b.id, t.id
        '''
    ).fetchall()
    return [dict(r) for r in rows]


def void_batch(conn: sqlite3.Connection, batch_id: int, *, reason: str, at: str) -> None:
    """作废一个戳记/扫描错误的批次：只改批次状态，业务回滚由调用方同事务完成。"""
    conn.execute(
        '''
        UPDATE apply_batches
        SET status='voided', voided_at=?, void_reason=?
        WHERE id=? AND status='done'
        ''',
        (at, reason, batch_id),
    )


def unreverted_voided_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """已作废但仍挂着已应用单/批次来源价签的批次：回滚没做干净。"""
    rows = conn.execute(
        '''
        SELECT b.id, b.batch_no
        FROM apply_batches b
        WHERE b.status='voided'
          AND (
            EXISTS (SELECT 1 FROM price_tickets t WHERE t.apply_batch_id=b.id AND t.status='applied')
            OR EXISTS (
              SELECT 1 FROM price_tickets t
              JOIN price_history h ON h.source_ticket_no = t.ticket_no
              WHERE t.apply_batch_id=b.id
            )
          )
        ORDER BY b.id
        '''
    ).fetchall()
    return [dict(r) for r in rows]
