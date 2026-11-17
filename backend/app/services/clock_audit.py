from __future__ import annotations

import sqlite3
from typing import Any

from app.db import connect
from app.repositories import batches as batch_repo
from app.repositories import prices as price_repo
from app.repositories import settings as settings_repo
from app.repositories import tickets as ticket_repo
from app.services import clock_plane

# 钟面分叉的唯一收口方式（全局只贯彻这一种）：
#   以设置表业务钟 settings.business_clock 为唯一当前钟。凡已成功落库却与
#   该钟矛盾的批次（批次戳记与扫描判定不同源，或戳记钟下根本未到点却被
#   应用），一律作废：回滚其价签/历史与单状态、批次标为 voided，禁止再被
#   沿用；随后「应用到期」按当前钟重扫生成新批次。
# 不采用「把批次 clock_at 反写回设置钟」的另一条收口——两种收口不得并存。
CLOCK_AUTHORITY = "settings.business_clock"


def batch_tickets(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    return ticket_repo.list_by_batch(conn, batch_id)


def batch_fork_evidence(conn: sqlite3.Connection, batch: dict[str, Any]) -> list[str]:
    """单个 done 批次与「同一扫描钟应得结果」的矛盾证据；空表示一致。"""
    evidence: list[str] = []
    clock_at = batch.get("clock_at")
    scan_clock = batch.get("scan_clock")

    # 1) 新批次同时记录扫描钟与戳记钟，两者必须同源相等。
    if scan_clock is not None and scan_clock != clock_at:
        evidence.append(f"批次戳记 {clock_at} ≠ 扫描判定钟 {scan_clock}")

    # 2) 抢扫：戳记钟尚未到点的单却被本批应用（历史批次也能凭单生效时刻互证）。
    for t in batch_tickets(conn, int(batch["id"])):
        if t["effective_at"] > clock_at:
            evidence.append(
                f"{t['ticket_no']} 生效 {t['effective_at']} 晚于批次钟 {clock_at}，未到点却被应用"
            )

    # 3) 漏扫：本批戳记钟下本已到期、且在本批开始前就已建好，却至今没被任何
    #    批应用的单。批次报「done」就等价于宣称「戳记钟下没有更多到期单」，
    #    这些单直接反驳它。created_at 用严格小于（截秒粒度），同秒视为并发
    #    建单、不算漏扫；已被后续批次扫走的单不再是 scheduled，不会误报。
    #    这一条同时覆盖历史遗留的「扫描按 T1、戳记写成 T2」方向：单的生效时刻
    #    落在 (T1,T2] 时，按戳记钟 T2 看它就是漏扫。
    missed = conn.execute(
        '''
        SELECT t.ticket_no, t.effective_at
        FROM price_tickets t
        WHERE t.status='scheduled'
          AND t.apply_batch_id IS NULL
          AND t.effective_at <= ?
          AND t.created_at < ?
        ORDER BY t.id
        ''',
        (clock_at, batch["created_at"]),
    ).fetchall()
    for r in missed:
        evidence.append(
            f"{r['ticket_no']} 在批次钟 {clock_at} 已到期且早于本批建单，本批却报成功未扫（漏扫）"
        )
    return evidence


def detect_forked_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """已成功（done）批次里与扫描钟矛盾者，附证据。只读。"""
    forked: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT id FROM apply_batches WHERE status='done' ORDER BY id"
    ).fetchall()
    for r in rows:
        batch = batch_repo.get_batch(conn, int(r["id"]))
        if not batch:
            continue
        evidence = batch_fork_evidence(conn, batch)
        if evidence:
            forked.append({"batch": batch, "evidence": evidence})
    return forked


def missed_due_tickets(conn: sqlite3.Connection, clock_at: str) -> list[str]:
    """当前业务钟下已到点却仍未 applied 的单号（漏扫证据，供重扫使用）。"""
    if not clock_at:
        return []
    return [t["ticket_no"] for t in ticket_repo.due_tickets(conn, clock_at)]


def snapshot_clocks() -> dict[str, Any]:
    """钟面一致性快照：设置键、扫描钟、展示针、对账钉扎必须指向同一时刻。"""
    conn = connect()
    try:
        business = settings_repo.get(conn, clock_plane.CLOCK_KEY)
        legacy = clock_plane.read_legacy_display_key(conn)
        scan = clock_plane.read_scan_clock()
        display = clock_plane.read_display_clock()
        forked = detect_forked_batches(conn)
        return {
            "authority": CLOCK_AUTHORITY,
            "business_clock": business,
            "scan_clock": scan,
            "display_clock": display,
            "pinned_clock": display,
            "legacy_ui_clock": legacy,
            "aligned": business == scan == display,
            "legacy_mirror_aligned": legacy is None or legacy == business,
            "forked_batches": [
                {
                    "batch_no": f["batch"]["batch_no"],
                    "clock_at": f["batch"]["clock_at"],
                    "scan_clock": f["batch"]["scan_clock"],
                    "tickets": [t["ticket_no"] for t in batch_tickets(conn, int(f["batch"]["id"]))],
                    "evidence": f["evidence"],
                }
                for f in forked
            ],
            "fork_count": len(forked),
            "missed_due_tickets": missed_due_tickets(conn, business),
            "unreverted_voided_batches": batch_repo.unreverted_voided_batches(conn),
        }
    finally:
        conn.close()


def rollback_forked_batch(
    conn: sqlite3.Connection, batch: dict[str, Any], *, at: str
) -> dict[str, Any]:
    """作废一个批次并回滚其价签/历史与单状态。须在调用方写事务内执行。"""
    reverted: list[str] = []
    for t in batch_tickets(conn, int(batch["id"])):
        price_repo.rollback_ticket(conn, t["ticket_no"])
        ticket_repo.revert_to_scheduled(conn, int(t["id"]))
        reverted.append(t["ticket_no"])
    batch_repo.void_batch(
        conn,
        int(batch["id"]),
        reason="钟面分叉：批次戳记/扫描与唯一业务钟矛盾，作废后按当前钟重扫",
        at=at,
    )
    return {"batch_no": batch["batch_no"], "reverted_tickets": reverted}
