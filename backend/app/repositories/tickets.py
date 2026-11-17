from __future__ import annotations

import sqlite3
from typing import Any


def next_ticket_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM price_tickets").fetchone()["c"] + 1
    return f"PT-{n:04d}"


def create_ticket(
    conn: sqlite3.Connection,
    *,
    ticket_no: str,
    title: str,
    effective_at: str,
    note: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO price_tickets(ticket_no, title, status, effective_at, note, created_at)
        VALUES (?,?,?,?,?,?)
        ''',
        (ticket_no, title, "scheduled", effective_at, note, created_at),
    )
    return int(cur.lastrowid)


def add_line(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    sku_code: str,
    old_price: float,
    new_price: float,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO price_ticket_lines(ticket_id, sku_code, old_price, new_price)
        VALUES (?,?,?,?)
        ''',
        (ticket_id, sku_code, old_price, new_price),
    )
    return int(cur.lastrowid)


def list_tickets(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT t.*,
               (SELECT COUNT(*) FROM price_ticket_lines l WHERE l.ticket_id=t.id) AS line_count
        FROM price_tickets t
        ORDER BY t.id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM price_tickets WHERE id=?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def list_lines(conn: sqlite3.Connection, ticket_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM price_ticket_lines WHERE ticket_id=? ORDER BY id",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def due_tickets(conn: sqlite3.Connection, clock_at: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT * FROM price_tickets
        WHERE status='scheduled' AND effective_at <= ?
        ORDER BY effective_at, id
        ''',
        (clock_at,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_applied(
    conn: sqlite3.Connection,
    ticket_id: int,
    *,
    applied_at: str,
    batch_id: int,
) -> None:
    conn.execute(
        '''
        UPDATE price_tickets
        SET status='applied', applied_at=?, apply_batch_id=?
        WHERE id=?
        ''',
        (applied_at, batch_id, ticket_id),
    )


def list_by_batch(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM price_tickets WHERE apply_batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def revert_to_scheduled(conn: sqlite3.Connection, ticket_id: int) -> None:
    """把单退回待生效：单状态、应用时刻、批次引用一并清空。"""
    conn.execute(
        '''
        UPDATE price_tickets
        SET status='scheduled', applied_at=NULL, apply_batch_id=NULL
        WHERE id=?
        ''',
        (ticket_id,),
    )


def due_but_not_applied(conn: sqlite3.Connection, clock_at: str) -> list[dict[str, Any]]:
    """当前钟下已到期却仍未 applied 的单（漏扫/待扫证据）。"""
    return due_tickets(conn, clock_at)
