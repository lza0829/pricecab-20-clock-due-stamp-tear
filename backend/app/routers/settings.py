from fastapi import APIRouter

from app.db import connect
from app.repositories import settings as settings_repo
from app.services import clock_audit

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def list_settings():
    conn = connect()
    try:
        return settings_repo.list_all(conn)
    finally:
        conn.close()


@router.get("/clock-audit")
def clock_audit_view():
    """钟面一致性视图（与 /api/apply/clock-audit 同一份只读快照）。"""
    return clock_audit.snapshot_clocks()
