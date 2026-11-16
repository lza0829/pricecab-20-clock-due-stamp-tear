from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="pricecab-tests-"))
os.environ["DATA_DIR"] = str(_TMP_ROOT)
os.environ.setdefault("APPLY_CONFLICT_POLICY", "apply_first")
os.environ["APPLY_LOCK_TIMEOUT_SECONDS"] = "30"
os.environ["APPLY_LOCK_WAIT_SECONDS"] = "3"
os.environ["CLOCK_LOCK_ACQUIRE_TIMEOUT"] = "0.2"
os.environ["CLOCK_PREEMPT_WAIT_SECONDS"] = "10"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.repositories import audit as audit_repo  # noqa: E402
from app.repositories import policies as policy_repo  # noqa: E402
from app.repositories import signals as signal_repo  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import apply_service as svc  # noqa: E402,F401


@pytest.fixture(scope="session", autouse=True)
def _seeded():
    seed_if_empty()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def reset_state(*, policy: str = "apply_first", lock_timeout: float = 30.0) -> None:
    """把业务数据恢复到种子状态，并清空锁、冲突与中止记录。"""
    audit_repo.clear()
    conn = connect(immediate=True)
    try:
        conn.execute("DELETE FROM price_ticket_lines")
        conn.execute("DELETE FROM price_tickets")
        conn.execute("DELETE FROM apply_batches")
        conn.execute("DELETE FROM price_history")
        conn.execute("DELETE FROM price_book")
        conn.execute(
            "UPDATE apply_lock SET holder='', holder_kind='', acquired_at='', expires_at='', "
            "acquired_epoch=0, expires_epoch=0, fence=fence+1, released_at=NULL WHERE id=1"
        )
        conn.execute("DELETE FROM settings WHERE key IN ('business_clock','apply_conflict_policy','apply_lock_timeout_seconds')")
        for row in conn.execute("SELECT sku_code, list_price FROM skus ORDER BY id").fetchall():
            conn.execute(
                "INSERT INTO price_book(sku_code, current_price, source_ticket_no, effective_at, updated_at) "
                "VALUES (?,?,NULL,'2026-11-16T08:00:00Z','2026-11-16T08:00:00Z')",
                (row["sku_code"], row["list_price"]),
            )
            conn.execute(
                "INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at) "
                "VALUES (?,?,NULL,'2026-11-16T08:00:00Z','2026-11-16T08:00:00Z')",
                (row["sku_code"], row["list_price"]),
            )
        conn.execute("INSERT INTO settings(key, value) VALUES ('business_clock', '2026-11-16T09:00:00Z')")
        policy_repo.set_value(conn, policy)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('apply_lock_timeout_seconds', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(lock_timeout),),
        )
        conn.commit()
    finally:
        conn.close()

    signal_repo.ensure_schema()
    sig_conn = signal_repo.connect(immediate=True, timeout=2.0)
    try:
        sig_conn.execute("UPDATE apply_preempt SET token='', source='', requested_at='', requested_epoch=0")
        sig_conn.commit()
    finally:
        sig_conn.close()


@pytest.fixture()
def clean():
    reset_state()
    yield
    reset_state()
