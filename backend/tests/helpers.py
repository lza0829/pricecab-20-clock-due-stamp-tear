from __future__ import annotations

from typing import Any

from app.db import connect
from app.repositories import locks as lock_repo


def lock_state() -> dict[str, Any]:
    conn = connect()
    try:
        return lock_repo.describe(conn)
    finally:
        conn.close()


def snapshot_state() -> dict[str, Any]:
    """一致性快照：用于断言不存在半截写入。"""
    conn = connect()
    try:
        applied = conn.execute(
            "SELECT COUNT(*) c FROM price_tickets WHERE status='applied'"
        ).fetchone()["c"]
        scheduled = conn.execute(
            "SELECT COUNT(*) c FROM price_tickets WHERE status='scheduled'"
        ).fetchone()["c"]

        # 单被标记 applied 却指向不存在/未完成的批次 => 半截写入。
        orphan_applied = conn.execute(
            '''
            SELECT COUNT(*) c
            FROM price_tickets t
            LEFT JOIN apply_batches b ON b.id = t.apply_batch_id
            WHERE t.status='applied'
              AND (t.apply_batch_id IS NULL OR b.id IS NULL OR b.status <> 'done')
            '''
        ).fetchone()["c"]

        # 已有改价单历史但单仍未 applied => 半截写入。
        written_but_not_applied = conn.execute(
            '''
            SELECT COUNT(*) c
            FROM price_tickets t
            WHERE t.status <> 'applied'
              AND EXISTS (
                SELECT 1 FROM price_history h WHERE h.source_ticket_no = t.ticket_no
              )
            '''
        ).fetchone()["c"]

        history_for_tickets = conn.execute(
            "SELECT COUNT(*) c FROM price_history WHERE source_ticket_no IS NOT NULL"
        ).fetchone()["c"]

        # 价签本与「最新一条历史」必须一致。
        mismatch = conn.execute(
            '''
            SELECT COUNT(*) c
            FROM price_book b
            JOIN (
              SELECT sku_code, price,
                     ROW_NUMBER() OVER (PARTITION BY sku_code ORDER BY effective_at DESC, id DESC) rn
              FROM price_history
            ) h ON h.sku_code = b.sku_code AND h.rn = 1
            WHERE ABS(b.current_price - h.price) > 1e-9
            '''
        ).fetchone()["c"]

        running_batches = conn.execute(
            "SELECT COUNT(*) c FROM apply_batches WHERE status <> 'done'"
        ).fetchone()["c"]

        return {
            "lock": lock_repo.describe(conn),
            "tickets": {"applied": applied, "scheduled": scheduled},
            "orphan_applied": orphan_applied,
            "written_but_not_applied": written_but_not_applied,
            "history_count_for_ticket": history_for_tickets,
            "book_matches_list": mismatch == 0,
            "running_batches": running_batches,
        }
    finally:
        conn.close()
