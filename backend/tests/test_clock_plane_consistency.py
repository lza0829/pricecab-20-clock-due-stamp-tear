from __future__ import annotations

import sqlite3

from app.db import connect
from app.repositories import prices as price_repo
from tests.conftest import reset_state


def make_ticket(client, *, sku="SKU-TEA-001", new_price=31.0, effective_at="2026-11-16T10:00:00Z"):
    resp = client.post(
        "/api/tickets",
        json={
            "title": "钟面分叉测试",
            "effective_at": effective_at,
            "lines": [{"sku_code": sku, "new_price": new_price}],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def price_of(client, sku="SKU-TEA-001") -> float:
    rows = client.get("/api/prices/book").json()
    return next(r["current_price"] for r in rows if r["sku_code"] == sku)


def settings_snapshot() -> dict[str, str]:
    conn = connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    finally:
        conn.close()


def rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def tamper_batch(batch_no: str, **fields) -> None:
    """直接改库，模拟历史遗留的戳记错误/漏扫批次。"""
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = connect(immediate=True)
    try:
        conn.execute(
            f"UPDATE apply_batches SET {sets} WHERE batch_no=?",
            (*fields.values(), batch_no),
        )
        conn.commit()
    finally:
        conn.close()


# --- 路径 1：页面/设置钟与扫描不一致 -------------------------------------


def test_page_clock_and_scan_clock_are_the_same_key(client, clean):
    """拨钟后，页面钟、扫描钟、对账钉扎、批次戳记必须是同一时刻。"""
    make_ticket(client, new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    moved = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["business_clock"] == "2026-11-16T10:30:00Z"
    assert moved.json()["clock_key"] == "business_clock"

    # 拨钟入口、页面总览、对账钉扎与审计视图读数一致。
    assert client.get("/api/apply/clock").json()["business_clock"] == "2026-11-16T10:30:00Z"
    assert client.get("/api/dashboard").json()["business_clock"] == "2026-11-16T10:30:00Z"
    assert client.get("/api/reconcile/sales").json()["pinned_clock"] == "2026-11-16T10:30:00Z"
    audit = client.get("/api/apply/clock-audit").json()
    assert audit["aligned"] is True, audit
    assert audit["scan_clock"] == audit["display_clock"] == audit["pinned_clock"] == audit["business_clock"]

    # 页面钟已过点 -> 应用就该扫到这张单，不允许「钟过点、单未进批」。
    assert client.get("/api/dashboard").json()["due_now"] == 1
    applied = client.post("/api/apply/due")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["applied"] is True
    assert body["business_clock"] == body["scan_clock"] == body["batch_clock_at"] == "2026-11-16T10:30:00Z"

    batch = client.get("/api/apply/batches").json()[0]
    assert batch["clock_at"] == batch["scan_clock"] == "2026-11-16T10:30:00Z"
    assert price_of(client) == 31.0


def test_legacy_ui_clock_divergence_does_not_drive_scan_or_stamp(client, clean):
    """历史残留 ui_clock=T2、business_clock=T1：扫描与戳记都只认业务钟。"""
    make_ticket(client, new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    conn = connect(immediate=True)
    try:
        conn.execute("UPDATE settings SET value='2026-11-16T09:00:00Z' WHERE key='business_clock'")
        conn.execute("UPDATE settings SET value='2026-11-16T23:00:00Z' WHERE key='ui_clock'")
        conn.commit()
    finally:
        conn.close()

    audit = client.get("/api/apply/clock-audit").json()
    assert audit["business_clock"] == "2026-11-16T09:00:00Z"
    assert audit["scan_clock"] == "2026-11-16T09:00:00Z"
    assert audit["pinned_clock"] == "2026-11-16T09:00:00Z"
    assert audit["legacy_mirror_aligned"] is False

    # 业务钟 09:00 尚未到 10:00：不得应用，也不得留下空跑成功批次。
    result = client.post("/api/apply/due").json()
    assert result["applied"] is False
    assert result["business_clock"] == "2026-11-16T09:00:00Z"
    assert client.get("/api/apply/batches").json() == []
    assert price_of(client) == 28.0

    # 按当前钟重扫前，遗留分叉也能被显式收口为单一业务钟。
    repaired = client.post("/api/apply/clock/repair").json()
    assert repaired["authority"] == "settings.business_clock"
    assert settings_snapshot()["ui_clock"] == "2026-11-16T09:00:00Z"


# --- 路径 2：批次戳 ≠ 扫描钟 --------------------------------------------


def test_batch_stamp_mismatching_clock_is_voided_and_rolled_back(client, clean):
    """扫描按 T1、戳记却写成 T2 的成功批次：作废批次并回滚价签与单态。"""
    make_ticket(client, new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})
    applied = client.post("/api/apply/due")
    assert applied.status_code == 200 and applied.json()["applied"] is True
    batch_no = applied.json()["batch_no"]
    assert price_of(client) == 31.0

    # 模拟分叉：戳记钟被写成另一个时刻（扫描仍按 10:30）。
    tamper_batch(batch_no, clock_at="2026-11-16T12:00:00Z")
    audit = client.get("/api/apply/clock-audit").json()
    assert audit["fork_count"] == 1, audit
    assert any("戳记" in e for e in audit["forked_batches"][0]["evidence"])

    repaired = client.post("/api/apply/clock/repair")
    assert repaired.status_code == 200, repaired.text
    body = repaired.json()
    assert [b["batch_no"] for b in body["repaired_batches"]] == [batch_no]
    assert body["rescan_required"] is True

    # 批次作废、价签/历史回滚、单退回待生效——不留半截 applied。
    batch = client.get("/api/apply/batches").json()[0]
    assert batch["status"] == "voided"
    assert batch["voided_at"]
    assert price_of(client) == 28.0
    assert rows("SELECT * FROM price_history WHERE source_ticket_no IS NOT NULL") == []
    ticket = client.get("/api/tickets").json()[0]
    assert ticket["status"] == "scheduled"
    assert ticket["apply_batch_id"] is None
    audit = client.get("/api/apply/clock-audit").json()
    assert audit["fork_count"] == 0
    assert audit["unreverted_voided_batches"] == []

    # 作废批不得被沿用；重扫会生成按当前钟戳记的新批次。
    again = client.post("/api/apply/due").json()
    assert again["applied"] is True
    assert again["batch_no"] != batch_no
    fresh = client.get("/api/apply/batches").json()[0]
    assert fresh["batch_no"] == again["batch_no"]
    assert fresh["status"] == "done"
    assert fresh["clock_at"] == fresh["scan_clock"] == "2026-11-16T10:30:00Z"
    assert price_of(client) == 31.0


def test_apply_rescans_after_fork_without_leaving_stale_batch(client, clean):
    """分叉未显式收口时，应用到期也必须先收口再按当前钟重扫。"""
    make_ticket(client, new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})
    first = client.post("/api/apply/due").json()
    tamper_batch(first["batch_no"], clock_at="2026-11-16T23:00:00Z")

    # 再建一张后到的单，一起验证重扫集合。
    make_ticket(client, sku="SKU-TEA-002", new_price=61.0, effective_at="2026-11-16T11:00:00Z")
    result = client.post("/api/apply/due")
    assert result.status_code == 200, result.text
    body = result.json()
    assert [b["batch_no"] for b in body["reverted_batches"]] == [first["batch_no"]]
    assert body["applied"] is True
    # 11:00 的单未到点，不能跟着重扫进来。
    assert body["tickets"] == [first["tickets"][0]]

    batches = client.get("/api/apply/batches").json()
    voided = next(b for b in batches if b["batch_no"] == first["batch_no"])
    fresh = next(b for b in batches if b["batch_no"] == body["batch_no"])
    assert voided["status"] == "voided"
    assert fresh["status"] == "done"
    # 同一张单只被应用的批次覆盖一次，没有「一单两批」的重复写入。
    assert fresh["linked_ticket_count"] == 1
    assert price_of(client) == 31.0
    assert price_of(client, "SKU-TEA-002") == 68.0


# --- 路径 3：漏扫 / 抢扫 -------------------------------------------------


def test_early_application_is_detected_and_rolled_back(client, clean):
    """钟未到点却被应用的批次（抢扫）：审计必须识别并作废回滚。"""
    make_ticket(client, new_price=31.0, effective_at="2026-11-16T22:00:00Z")
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T23:00:00Z"})
    applied = client.post("/api/apply/due").json()
    assert applied["applied"] is True

    # 模拟遗留抢扫：把批次钟改到早于单生效时刻，凑成「未到点却已应用」。
    tamper_batch(applied["batch_no"], clock_at="2026-11-16T21:00:00Z", scan_clock="2026-11-16T21:00:00Z")
    audit = client.get("/api/apply/clock-audit").json()
    assert audit["fork_count"] == 1
    assert any("未到点却被应用" in e for e in audit["forked_batches"][0]["evidence"])

    repaired = client.post("/api/apply/clock/repair").json()
    assert repaired["rescan_required"] is True
    assert price_of(client) == 28.0
    assert client.get("/api/tickets").json()[0]["status"] == "scheduled"

    # 收口后按当前钟重扫：23:00 已过 22:00，应正常应用且戳记为当前钟。
    again = client.post("/api/apply/due").json()
    assert again["applied"] is True
    assert again["batch_clock_at"] == "2026-11-16T23:00:00Z"
    assert price_of(client) == 31.0


def test_due_ticket_is_never_silently_skipped(client, clean):
    """已到点的单不得漏扫：应用要么写入，要么明确没有任何到期单。"""
    make_ticket(client, new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})

    audit = client.get("/api/apply/clock-audit").json()
    assert audit["missed_due_tickets"] == ["PT-0001"]

    before = client.post("/api/apply/due").json()
    assert before["applied"] is True
    assert before["tickets"] == ["PT-0001"]
    assert client.get("/api/apply/clock-audit").json()["missed_due_tickets"] == []

    # 已扫过的单不会在第二天被当成「空跑成功」重复入批。
    empty = client.post("/api/apply/due")
    assert empty.status_code == 200
    assert empty.json()["applied"] is False
    assert empty.json()["ticket_count"] == 0
    assert len(client.get("/api/apply/batches").json()) == 1


def test_legacy_scan_behind_stamp_is_caught_as_missed_scan(client, clean):
    """历史遗留「扫描按 T1、戳记写成 T2」：T1~T2 之间到期的单被漏扫，审计可证。"""
    import time

    # 两张单都在批次之前建好；10:00 扫描时 10:15 的单尚未到期，因此不在集合里。
    make_ticket(client, sku="SKU-TEA-001", new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    make_ticket(client, sku="SKU-TEA-002", new_price=61.0, effective_at="2026-11-16T10:15:00Z")
    # 时间戳只到秒：等过一秒，保证「单早于批次建」在库里可证。
    time.sleep(1.1)
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:00:00Z"})
    first = client.post("/api/apply/due").json()
    assert first["applied"] is True
    assert first["tickets"] == ["PT-0001"]

    # 仅改戳记（扫描钟列是后加的，历史批次为 NULL），模拟遗留分叉。
    tamper_batch(first["batch_no"], clock_at="2026-11-16T10:30:00Z", scan_clock=None)

    audit = client.get("/api/apply/clock-audit").json()
    assert audit["fork_count"] == 1, audit
    evidence = audit["forked_batches"][0]["evidence"]
    assert any("漏扫" in e for e in evidence), evidence

    repaired = client.post("/api/apply/clock/repair").json()
    assert repaired["repaired_batches"][0]["batch_no"] == first["batch_no"]
    assert price_of(client) == 28.0

    again = client.post("/api/apply/due").json()
    assert again["applied"] is True
    # 当前生效钟是 10:00：重扫只应写入 10:00 的单，10:15 的仍待生效。
    assert again["tickets"] == ["PT-0001"]
    assert again["batch_clock_at"] == "2026-11-16T10:00:00Z"
    assert price_of(client) == 31.0
    assert price_of(client, "SKU-TEA-002") == 68.0

    # 把钟拨到 10:30 再应用，10:15 的单才到期并进入新批次。
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})
    late = client.post("/api/apply/due").json()
    assert late["tickets"] == ["PT-0002"]
    assert late["batch_clock_at"] == "2026-11-16T10:30:00Z"
    assert price_of(client, "SKU-TEA-002") == 61.0
    assert client.get("/api/apply/clock-audit").json()["fork_count"] == 0


def test_healthy_multi_batch_history_is_not_flagged_as_fork(client, clean):
    """按当前钟分两次正常应用：不得把先前的批次误判为漏扫/分叉。"""
    make_ticket(client, sku="SKU-TEA-001", new_price=31.0, effective_at="2026-11-16T10:00:00Z")
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})
    assert client.post("/api/apply/due").json()["applied"] is True

    make_ticket(client, sku="SKU-TEA-002", new_price=61.0, effective_at="2026-11-16T11:00:00Z")
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T11:30:00Z"})
    assert client.post("/api/apply/due").json()["applied"] is True

    audit = client.get("/api/apply/clock-audit").json()
    assert audit["fork_count"] == 0, audit
    assert audit["missed_due_tickets"] == []
    assert audit["aligned"] is True
    assert client.get("/api/apply/batches").json()[0]["status"] == "done"


def test_forced_clock_write_only_moves_one_key(client, clean):
    """拨钟入口不得再写出第二个「当前钟」：设置键在拨钟后立即一致。"""
    moved = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T14:00:00Z"})
    assert moved.status_code == 200
    settings = settings_snapshot()
    assert settings["business_clock"] == "2026-11-16T14:00:00Z"
    assert settings["ui_clock"] == "2026-11-16T14:00:00Z"
    audit = client.get("/api/settings/clock-audit").json()
    assert audit["aligned"] is True
    assert audit["legacy_mirror_aligned"] is True
