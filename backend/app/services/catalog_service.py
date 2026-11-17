from __future__ import annotations

from typing import Any

from app.db import connect
from app.repositories import skus as sku_repo
from app.repositories import tickets as ticket_repo
from app.services import clock_audit, clock_plane


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        # 页面业务钟与到期扫描读同一个设置键，页面上「已过」与「可应用」才一致。
        clock = clock_plane.read_clock(conn)
        return {
            "project": "pricecab",
            "name": "价签柜",
            "business_clock": clock,
            "scan_clock": clock,
            "clock_key": clock_plane.CLOCK_KEY,
            "clock_fork": clock_audit.snapshot_clocks()["fork_count"],
            "sku_count": conn.execute("SELECT COUNT(*) c FROM skus").fetchone()["c"],
            "ticket_count": conn.execute("SELECT COUNT(*) c FROM price_tickets").fetchone()["c"],
            "scheduled_count": conn.execute(
                "SELECT COUNT(*) c FROM price_tickets WHERE status='scheduled'"
            ).fetchone()["c"],
            "applied_count": conn.execute(
                "SELECT COUNT(*) c FROM price_tickets WHERE status='applied'"
            ).fetchone()["c"],
            "book_count": conn.execute("SELECT COUNT(*) c FROM price_book").fetchone()["c"],
            "sale_count": conn.execute("SELECT COUNT(*) c FROM sales_fixture").fetchone()["c"],
            "batch_count": conn.execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"],
            "due_now": len(ticket_repo.due_tickets(conn, clock or "")),
        }
    finally:
        conn.close()


def list_skus() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return sku_repo.list_skus(conn)
    finally:
        conn.close()
