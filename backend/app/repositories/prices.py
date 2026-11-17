from __future__ import annotations

import sqlite3
from typing import Any


def list_book(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT b.*, s.title, s.category
        FROM price_book b
        JOIN skus s ON s.sku_code = b.sku_code
        ORDER BY b.sku_code
        '''
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_book(
    conn: sqlite3.Connection,
    *,
    sku_code: str,
    price: float,
    ticket_no: str,
    effective_at: str,
    updated_at: str,
) -> None:
    conn.execute(
        '''
        INSERT INTO price_book(sku_code, current_price, source_ticket_no, effective_at, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(sku_code) DO UPDATE SET
          current_price=excluded.current_price,
          source_ticket_no=excluded.source_ticket_no,
          effective_at=excluded.effective_at,
          updated_at=excluded.updated_at
        ''',
        (sku_code, price, ticket_no, effective_at, updated_at),
    )
    conn.execute(
        '''
        INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at)
        VALUES (?,?,?,?,?)
        ''',
        (sku_code, price, ticket_no, effective_at, updated_at),
    )


def list_history(conn: sqlite3.Connection, sku_code: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if sku_code:
        rows = conn.execute(
            "SELECT * FROM price_history WHERE sku_code=? ORDER BY id DESC LIMIT ?",
            (sku_code, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM price_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def price_at(conn: sqlite3.Connection, sku_code: str, at: str) -> float | None:
    row = conn.execute(
        '''
        SELECT price FROM price_history
        WHERE sku_code=? AND effective_at <= ?
        ORDER BY effective_at DESC, id DESC
        LIMIT 1
        ''',
        (sku_code, at),
    ).fetchone()
    return float(row["price"]) if row else None


def history_for_ticket(conn: sqlite3.Connection, ticket_no: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM price_history WHERE source_ticket_no=? ORDER BY id",
        (ticket_no,),
    ).fetchall()
    return [dict(r) for r in rows]


def rollback_ticket(conn: sqlite3.Connection, ticket_no: str) -> list[str]:
    """回滚一张单写入的价签/历史，返回受影响的 SKU。

    判据只认「这条历史属于该单」：删掉该单的全部历史行，再把价签本回填为
    剩余历史里最新的那一条；若没有剩余历史则保持价签不动（不应发生：种子
    价签自带一条历史）。不能用「当前价 == 单新价」之类的值相等判断，否则
    同价改价单的回滚会误删他人的价签。
    """
    affected = sorted({r["sku_code"] for r in history_for_ticket(conn, ticket_no)})
    conn.execute("DELETE FROM price_history WHERE source_ticket_no=?", (ticket_no,))
    for sku in affected:
        latest = conn.execute(
            '''
            SELECT price, source_ticket_no, effective_at, recorded_at
            FROM price_history
            WHERE sku_code=?
            ORDER BY effective_at DESC, id DESC
            LIMIT 1
            ''',
            (sku,),
        ).fetchone()
        if latest is None:
            # 无任何历史可依据时，退回商品标价，避免价签指向已作废的单。
            conn.execute(
                "UPDATE price_book SET current_price=(SELECT list_price FROM skus WHERE sku_code=?), "
                "source_ticket_no=NULL, effective_at=NULL, updated_at=? WHERE sku_code=?",
                (sku, _utc_now(), sku),
            )
            continue
        conn.execute(
            '''
            UPDATE price_book
            SET current_price=?, source_ticket_no=?, effective_at=?, updated_at=?
            WHERE sku_code=?
            ''',
            (float(latest["price"]), latest["source_ticket_no"], latest["effective_at"],
             latest["recorded_at"], sku),
        )
    return affected


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
