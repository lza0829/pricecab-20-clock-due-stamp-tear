from __future__ import annotations

import threading
import time

import pytest

from app import config
from app.db import connect
from tests.helpers import snapshot_state
from tests.conftest import reset_state


def make_ticket(client, *, sku="SKU-TEA-001", new_price=31.0, effective_at="2026-11-16T10:00:00Z"):
    resp = client.post(
        "/api/tickets",
        json={
            "title": "测试改价单",
            "effective_at": effective_at,
            "lines": [{"sku_code": sku, "new_price": new_price}],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def price_of(client, sku="SKU-TEA-001") -> float:
    rows = client.get("/api/prices/book").json()
    return next(r["current_price"] for r in rows if r["sku_code"] == sku)


def clock_of(client) -> str:
    return client.get("/api/apply/clock").json()["business_clock"]


def test_apply_priority_rejects_clock_and_keeps_old_clock_price(client, clean):
    reset_state(policy="apply_first")
    make_ticket(client, new_price=31.0)
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})
    assert clock_of(client) == "2026-11-16T10:30:00Z"

    result: dict = {}

    def run_apply():
        result["resp"] = client.post("/api/apply/due?hold_ms=1500")

    t = threading.Thread(target=run_apply)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        st = client.get("/api/apply/lock").json()
        if st.get("held") and (st.get("lock") or {}).get("holder_kind") == "apply":
            break
        time.sleep(0.02)
    else:
        t.join()
        pytest.fail("应用未持锁")

    conflict = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T23:00:00Z"})
    assert conflict.status_code == 409, conflict.text
    assert "业务钟未修改" in conflict.text
    assert clock_of(client) == "2026-11-16T10:30:00Z"

    t.join()
    assert result["resp"].status_code == 200, result["resp"].text
    assert price_of(client) == 31.0
    batches = client.get("/api/apply/batches").json()
    assert len(batches) == 1 and batches[0]["status"] == "done"
    assert batches[0]["clock_at"] == "2026-11-16T10:30:00Z"

    snap = snapshot_state()
    assert not snap["lock"]["held"]
    assert snap["tickets"]["applied"] == 1 and snap["tickets"]["scheduled"] == 0
    assert snap["orphan_applied"] == 0


def test_clock_priority_aborts_apply_and_lands_new_clock(client, clean):
    reset_state(policy="clock_first")
    make_ticket(client, new_price=33.0)
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})

    result: dict = {}

    def run_apply():
        result["resp"] = client.post("/api/apply/due?hold_ms=800")

    t = threading.Thread(target=run_apply)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        st = client.get("/api/apply/lock").json()
        if st.get("held") and (st.get("lock") or {}).get("holder_kind") == "apply":
            break
        time.sleep(0.02)
    else:
        t.join()
        pytest.fail("应用未持锁")

    moved = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T12:00:00Z"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["business_clock"] == "2026-11-16T12:00:00Z"

    t.join()
    assert result["resp"].status_code == 409, result["resp"].text
    assert "回滚" in result["resp"].text

    assert price_of(client) == 28.0
    assert clock_of(client) == "2026-11-16T12:00:00Z"
    batches = client.get("/api/apply/batches").json()
    assert batches == []

    snap = snapshot_state()
    assert snap["tickets"]["applied"] == 0
    assert snap["tickets"]["scheduled"] == 1
    assert snap["orphan_applied"] == 0
    assert not snap["lock"]["held"]
    aborts = client.get("/api/apply/aborts").json()
    assert aborts and aborts[0]["rolled_back"] == 1


def test_double_open_apply_is_mutually_exclusive(client, clean):
    reset_state(policy="apply_first")
    make_ticket(client, new_price=35.0)
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})

    first: dict = {}

    def run_apply():
        first["resp"] = client.post("/api/apply/due?hold_ms=1200")

    t = threading.Thread(target=run_apply)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        st = client.get("/api/apply/lock").json()
        if st.get("held") and (st.get("lock") or {}).get("holder_kind") == "apply":
            break
        time.sleep(0.02)
    else:
        t.join()
        pytest.fail("应用未持锁")

    second = client.post("/api/apply/due")
    assert second.status_code == 409, second.text
    assert "双开应用互斥" in second.text

    t.join()
    assert first["resp"].status_code == 200, first["resp"].text
    batches = client.get("/api/apply/batches").json()
    assert len(batches) == 1
    assert batches[0]["status"] == "done"
    snap = snapshot_state()
    assert snap["tickets"]["applied"] == 1
    assert snap["orphan_applied"] == 0
    assert snap["history_count_for_ticket"] == 1


def test_apply_lock_released_after_timeout_without_half_write(client, clean):
    reset_state(policy="apply_first", lock_timeout=1.0)
    make_ticket(client, new_price=27.5)
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})

    result: dict = {}

    def run_apply():
        result["resp"] = client.post("/api/apply/due?hold_ms=2600")

    t = threading.Thread(target=run_apply)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        st = client.get("/api/apply/lock").json()
        if st.get("held") and (st.get("lock") or {}).get("holder_kind") == "apply":
            break
        time.sleep(0.02)
    else:
        t.join()
        pytest.fail("应用未持锁")

    time.sleep(1.2)
    st = client.get("/api/apply/lock").json()
    assert st["stale"] is True, st

    recovered = client.post("/api/apply/lock/recover").json()
    assert recovered["held"] is False, recovered

    t.join()
    assert result["resp"].status_code == 409, result["resp"].text
    assert "持锁超时" in result["resp"].text

    snap = snapshot_state()
    assert snap["tickets"]["applied"] == 0, snap
    assert snap["tickets"]["scheduled"] == 1
    assert snap["orphan_applied"] == 0
    assert snap["history_count_for_ticket"] == 0
    assert snap["book_matches_list"] is True
    assert not snap["lock"]["held"]

    again = client.post("/api/apply/due")
    assert again.status_code == 200, again.text
    assert again.json()["applied"] is True
    assert price_of(client) == 27.5
    snap = snapshot_state()
    assert snap["tickets"]["applied"] == 1
    assert snap["orphan_applied"] == 0


def test_only_one_policy_is_effective(client, clean):
    reset_state(policy="apply_first")
    assert client.get("/api/apply/policy").json()["policy"] == "apply_first"
    switched = client.put("/api/apply/policy", json={"policy": "clock_first"})
    assert switched.status_code == 200
    assert switched.json()["policy"] == "clock_first"
    seeded_conn = connect()
    try:
        from app.repositories import policies as policy_repo
        assert policy_repo.get(seeded_conn)["policy"] == "clock_first"
    finally:
        seeded_conn.close()
    bad = client.put("/api/apply/policy", json={"policy": "both"})
    assert bad.status_code == 400
    assert client.put("/api/apply/policy", json={"policy": "apply_first"}).status_code == 200


def test_clock_priority_mid_write_aborts_and_rolls_back_partial_lines(client, clean):
    """拨钟落在应用写到一半时：已写价签/历史必须整批回滚，不得半截 applied。"""
    reset_state(policy="clock_first")
    resp = client.post(
        "/api/tickets",
        json={
            "title": "多行改价单",
            "effective_at": "2026-11-16T10:00:00Z",
            "lines": [
                {"sku_code": "SKU-TEA-001", "new_price": 21.0},
                {"sku_code": "SKU-TEA-002", "new_price": 61.0},
                {"sku_code": "SKU-SNK-011", "new_price": 9.9},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})

    result: dict = {}

    def run_apply():
        # 每行写完后停 600ms，给拨钟落在「写到一半」的机会。
        result["resp"] = client.post("/api/apply/due?line_ms=600")

    t = threading.Thread(target=run_apply)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        st = client.get("/api/apply/lock").json()
        if st.get("held") and (st.get("lock") or {}).get("holder_kind") == "apply":
            break
        time.sleep(0.02)
    else:
        t.join()
        pytest.fail("应用未持锁")

    time.sleep(0.5)  # 让应用至少写完一行
    moved = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T13:00:00Z"})
    assert moved.status_code == 200, moved.text

    t.join()
    assert result["resp"].status_code == 409, result["resp"].text

    snap = snapshot_state()
    assert snap["tickets"]["applied"] == 0
    assert snap["tickets"]["scheduled"] == 1
    assert snap["orphan_applied"] == 0
    assert snap["written_but_not_applied"] == 0
    assert snap["history_count_for_ticket"] == 0
    assert snap["book_matches_list"] is True
    assert snap["running_batches"] == 0

    aborts = client.get("/api/apply/aborts").json()
    assert aborts, "应记录中止回执"
    assert aborts[0]["written_lines"] >= 1, aborts[0]
    assert aborts[0]["rolled_back"] == 1

    # 新钟落库；后续再应用时按新钟把这张单正常写入。
    assert clock_of(client) == "2026-11-16T13:00:00Z"
    again = client.post("/api/apply/due")
    assert again.status_code == 200, again.text
    assert again.json()["applied"] is True
    snap = snapshot_state()
    assert snap["tickets"]["applied"] == 1
    assert snap["orphan_applied"] == 0
    prices = {r["sku_code"]: r["current_price"] for r in client.get("/api/prices/book").json()}
    assert prices["SKU-TEA-001"] == 21.0
    assert prices["SKU-TEA-002"] == 61.0
    assert prices["SKU-SNK-011"] == 9.9


def test_clock_rejection_does_not_block_later_clock_after_apply(client, clean):
    """应用优先：应用期间的拨钟被拒，应用完成后同一拨钟可以成功。"""
    reset_state(policy="apply_first")
    make_ticket(client, new_price=29.0)
    client.put("/api/apply/clock", json={"business_clock": "2026-11-16T10:30:00Z"})

    result: dict = {}

    def run_apply():
        result["resp"] = client.post("/api/apply/due?hold_ms=1200")

    t = threading.Thread(target=run_apply)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        st = client.get("/api/apply/lock").json()
        if st.get("held") and (st.get("lock") or {}).get("holder_kind") == "apply":
            break
        time.sleep(0.02)
    else:
        t.join()
        pytest.fail("应用未持锁")

    rejected = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T22:00:00Z"})
    assert rejected.status_code == 409
    t.join()
    assert result["resp"].status_code == 200

    applied = client.put("/api/apply/clock", json={"business_clock": "2026-11-16T22:00:00Z"})
    assert applied.status_code == 200, applied.text
    assert applied.json()["business_clock"] == "2026-11-16T22:00:00Z"
    assert price_of(client) == 29.0
