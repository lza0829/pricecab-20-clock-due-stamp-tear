from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services import apply_service as svc

router = APIRouter(prefix="/apply", tags=["apply"])


class ClockIn(BaseModel):
    business_clock: str


class PolicyIn(BaseModel):
    policy: str
    lock_timeout_seconds: float | None = None


@router.get("/clock")
def get_clock():
    return svc.get_clock()


@router.put("/clock")
def set_clock(body: ClockIn):
    return svc.set_clock(body.business_clock)


@router.get("/batches")
def list_batches():
    return svc.list_batches()


@router.post("/due")
def apply_due(
    hold_ms: int | None = Query(
        default=None,
        ge=0,
        le=60000,
        description="演示/测试用：持锁推进耗时（毫秒），超过锁超时会触发整批回滚",
    ),
    line_ms: int | None = Query(
        default=None,
        ge=0,
        le=5000,
        description="演示/测试用：每写一条明细后的停留（毫秒）",
    ),
):
    return svc.apply_due(hold_ms=hold_ms, line_ms=line_ms)


@router.get("/lock")
def lock_status():
    return svc.lock_status()


@router.post("/lock/recover")
def recover_lock():
    return svc.recover_stale_lock()


@router.get("/policy")
def get_policy():
    return svc.lock_status()


@router.put("/policy")
def set_policy(body: PolicyIn):
    return svc.set_policy(body.policy, body.lock_timeout_seconds)


@router.get("/conflicts")
def list_conflicts(limit: int = Query(default=20, ge=1, le=200)):
    return svc.list_conflicts(limit)


@router.get("/aborts")
def list_aborts(limit: int = Query(default=20, ge=1, le=200)):
    return svc.list_aborts(limit)
