from __future__ import annotations

import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app import config
from app.db import connect
from app.repositories import audit as audit_repo
from app.repositories import batches as batch_repo
from app.repositories import locks as lock_repo
from app.repositories import policies as policy_repo
from app.repositories import prices as price_repo
from app.repositories import settings as settings_repo
from app.repositories import signals as signal_repo
from app.repositories import tickets as ticket_repo


# 拨钟临界区只做一次读-判-写，持有时间极短。
CLOCK_LOCK_TTL_SECONDS = 5.0
LOCK_TIMEOUT_KEY = "apply_lock_timeout_seconds"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_holder(kind: str) -> str:
    return f"{kind}-{secrets.token_hex(6)}"


def lock_timeout_seconds(conn: sqlite3.Connection) -> float:
    """锁超时阈值：可运行期调整（测试/演示用），下限 1 秒。"""
    raw = settings_repo.get(conn, LOCK_TIMEOUT_KEY)
    try:
        value = float(raw) if raw is not None else config.APPLY_LOCK_TIMEOUT_SECONDS
    except ValueError:
        value = config.APPLY_LOCK_TIMEOUT_SECONDS
    return max(value, 1.0)


class _AbortBatch(Exception):
    """应用在持锁期间被中止（拨钟抢占或锁已超时被回收）。"""

    def __init__(self, reason: str, *, winner: str, written_lines: int = 0):
        self.reason = reason
        self.winner = winner
        self.written_lines = written_lines
        super().__init__(reason)


# --- 回执 / 审计 -----------------------------------------------------------


def _record_conflict(*, kind: str, policy: str, winner: str, loser: str, detail: str) -> None:
    """冲突回执落一条审计记录；审计失败不影响主流程的判定与报错。"""
    audit_repo.record_conflict(
        kind=kind, policy=policy, winner=winner, loser=loser, detail=detail, created_at=_now()
    )


def _record_abort(
    *,
    holder: str,
    fence: int,
    reason: str,
    policy: str,
    written_lines: int,
) -> None:
    """业务写入回滚之后记中止回执；回执失败不影响回滚结果。"""
    audit_repo.record_abort(
        holder=holder,
        fence=fence,
        reason=reason,
        policy=policy,
        written_lines=written_lines,
        created_at=_now(),
    )


# --- 只读查询 -------------------------------------------------------------


def get_clock() -> dict[str, Any]:
    conn = connect()
    try:
        policy = policy_repo.get(conn)
        return {
            "business_clock": settings_repo.get(conn, "ui_clock") or settings_repo.get(conn, "business_clock"),
            "clock_label": settings_repo.get(conn, "clock_label"),
            "policy": policy["policy"],
            "policy_label": policy["policy_label"],
            "lock_timeout_seconds": lock_timeout_seconds(conn),
            "lock_wait_seconds": config.APPLY_LOCK_WAIT_SECONDS,
        }
    finally:
        conn.close()


def lock_status() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            **policy_repo.get(conn),
            "lock_timeout_seconds": lock_timeout_seconds(conn),
            "lock_wait_seconds": config.APPLY_LOCK_WAIT_SECONDS,
            **lock_repo.describe(conn),
        }
    finally:
        conn.close()


def list_conflicts(limit: int = 20) -> list[dict[str, Any]]:
    return audit_repo.list_conflicts(limit)


def list_aborts(limit: int = 20) -> list[dict[str, Any]]:
    return audit_repo.list_aborts(limit)


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_batches(conn)
    finally:
        conn.close()


def set_policy(policy: str, lock_timeout: float | None = None) -> dict[str, Any]:
    """切换全局让路策略并可选调整锁超时。策略一次只有一种生效值。"""
    try:
        timeout_value = None if lock_timeout is None else max(float(lock_timeout), 1.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "锁超时必须是数字") from exc

    conn = connect(immediate=True)
    try:
        policy_repo.set_value(conn, policy)
        if timeout_value is not None:
            settings_repo.set_value(conn, LOCK_TIMEOUT_KEY, str(timeout_value))
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()
    return lock_status()


def recover_stale_lock() -> dict[str, Any]:
    """显式回收一把超时未完成的锁（只清锁，不触碰任何业务数据）。"""
    holder = _new_holder("recover")
    try:
        conn = connect(immediate=True, timeout=1.0)
    except sqlite3.OperationalError as exc:
        raise HTTPException(409, "生效临界区正被写事务占用，稍后再回收") from exc
    try:
        policy = policy_repo.get(conn)["policy"]
        stale = lock_repo.recover_stale(conn)
        conn.commit()
    finally:
        conn.close()

    if stale:
        _record_conflict(
            kind="lock_recovered",
            policy=policy,
            winner=holder,
            loser=stale["holder"],
            detail="显式回收超时未释放的生效临界区锁",
        )
    return lock_status()


# --- 拨钟 -----------------------------------------------------------------


def _peek_lock() -> dict[str, Any] | None:
    try:
        conn = connect(timeout=0.2)
        try:
            return lock_repo.get_lock(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _reject_clock(detail: str, *, policy: str, winner: str) -> None:
    _record_conflict(
        kind="clock_rejected", policy=policy, winner=winner, loser="clock", detail=detail
    )
    raise HTTPException(
        409,
        f"{config.POLICY_LABELS[policy]}：{detail}；业务钟未修改，应用批次不受影响",
    )


def _reject_apply(detail: str, *, policy: str, winner: str) -> None:
    _record_conflict(
        kind="apply_rejected", policy=policy, winner=winner, loser="apply", detail=detail
    )
    raise HTTPException(
        409,
        f"{config.POLICY_LABELS[policy]}：{detail}；本批已回滚，未残留价签或单状态",
    )


def _post_preempt_signal(token: str, holder: str, *, deadline: float) -> bool:
    """登记拨钟中止信号（拨钟优先）。

    信号库写事务必须拿到，才能保证信号先于应用的提交落库。应用在提交
    窗口内会短暂持有该写事务，这里等它让出；拿不到则视为冲突。
    """
    signal_repo.ensure_schema()
    while True:
        try:
            conn = signal_repo.connect(
                immediate=True, timeout=max(config.CLOCK_LOCK_ACQUIRE_TIMEOUT, 0.1)
            )
        except sqlite3.OperationalError:
            if time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            return False
        try:
            signal_repo.write_preempt(
                conn,
                token=token,
                source=holder,
                requested_at=_now(),
                requested_epoch=time.time(),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def _preempted(policy: str) -> dict[str, Any] | None:
    """应用在写入阶段之间读取中止信号（只读窥探）。"""
    if policy != "clock_first":
        return None
    sig = signal_repo.read_preempt_standalone()
    if not _signal_fresh(sig):
        return None
    return sig


def _clear_preempt(token: str) -> None:
    signal_repo.ensure_schema()
    try:
        conn = signal_repo.connect(immediate=True, timeout=2.0)
        try:
            signal_repo.clear_preempt(conn, token)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        # 残留信号有有效期，不会误伤后续批次。
        pass


def _current_policy() -> str:
    conn = connect(timeout=1.0)
    try:
        return policy_repo.get(conn)["policy"]
    finally:
        conn.close()


def set_clock(clock_at: str) -> dict[str, Any]:
    """拨业务钟。与「应用到期」共享生效临界区。

    - 应用优先：应用持锁期间一律立即失败，不改钟、不迫使应用回滚。
    - 拨钟优先：先登记中止信号，应用在写入阶段之间察觉并整批回滚；
      随后拨钟获得锁并生效。
    """
    clock_at = (clock_at or "").strip()
    if not clock_at:
        raise HTTPException(400, "业务钟不能为空")

    policy = _current_policy()
    holder = _new_holder("clock")
    token = secrets.token_hex(8)
    clock_first = policy == "clock_first"

    if clock_first:
        # 先登记中止信号。应用即使正持有主库写事务也能读到并中止；
        # 信号库的写事务同时充当提交闸，见 _run_apply 的闸 3。
        if not _post_preempt_signal(token, holder, deadline=time.monotonic() + 5.0):
            _reject_clock(
                "应用正在提交，拨钟未能登记中止信号",
                policy=policy,
                winner="apply",
            )

    # 拨钟优先时要等应用察觉信号并回滚；预算覆盖一次完整锁超时 + 余量。
    wait_budget = 0.0
    if clock_first:
        conn_budget = connect(timeout=1.0)
        try:
            wait_budget = max(
                config.CLOCK_PREEMPT_WAIT_SECONDS,
                lock_timeout_seconds(conn_budget) + 5.0,
            )
        finally:
            conn_budget.close()
    deadline = time.monotonic() + wait_budget
    try:
        while True:
            try:
                conn = connect(immediate=True, timeout=config.CLOCK_LOCK_ACQUIRE_TIMEOUT)
            except sqlite3.OperationalError:
                row = _peek_lock()
                retryable = clock_first and time.monotonic() < deadline and (
                    row is None
                    or lock_repo.is_expired(row)
                    or (row or {}).get("holder_kind") == "apply"
                )
                if retryable:
                    time.sleep(0.05)
                    continue
                _reject_clock(
                    "应用正在持锁推进，拨钟被拒绝",
                    policy=policy,
                    winner=(row or {}).get("holder") or "apply",
                )

            try:
                current = lock_repo.get_lock(conn)
                if current and not lock_repo.is_expired(current):
                    kind = current.get("holder_kind") or "unknown"
                    winner = current["holder"]
                    conn.rollback()
                    if clock_first and time.monotonic() < deadline:
                        # 应用正在察觉信号并回滚，稍候再试。
                        time.sleep(0.05)
                        continue
                    detail = (
                        "应用正在持锁推进，拨钟被拒绝"
                        if kind == "apply"
                        else "生效临界区已被占用，拨钟被拒绝"
                    )
                    _reject_clock(detail, policy=policy, winner=winner)

                recovered = None
                if current and lock_repo.is_expired(current):
                    # 超时未完成：回收并把栅栏 +1，原持有者提交前复核必然失败。
                    recovered = lock_repo.recover_stale(conn)

                fence = lock_repo.acquire(
                    conn, holder=holder, kind="clock", ttl_seconds=CLOCK_LOCK_TTL_SECONDS
                )
                settings_repo.set_value(conn, "ui_clock", clock_at)
                lock_repo.release(conn, holder, fence)
                conn.commit()
            except HTTPException:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

            if recovered:
                _record_conflict(
                    kind="lock_recovered",
                    policy=policy,
                    winner=holder,
                    loser=recovered["holder"],
                    detail="锁超时未完成，拨钟回收后继续",
                )
            return get_clock()
    finally:
        if clock_first:
            _clear_preempt(token)


# --- 应用到期 -------------------------------------------------------------


def _acquire_apply_lock(holder: str, policy: str) -> tuple[int, float]:
    """获取应用锁并提交，进入临界区。

    - 另一应用持锁且未超时：立即冲突（双开应用互斥），不等待、不抢租约。
    - 拨钟持锁：短临界区，短暂让路重试。
    - 锁已超时：回收（栅栏 +1）后获取；原持有者提交前复核会失败并整批回滚。
    """
    deadline = time.monotonic() + config.APPLY_LOCK_WAIT_SECONDS
    while True:
        try:
            conn = connect(immediate=True, timeout=config.CLOCK_LOCK_ACQUIRE_TIMEOUT)
        except sqlite3.OperationalError:
            row = _peek_lock()
            can_retry = time.monotonic() < deadline and (
                row is None
                or lock_repo.is_expired(row)
                or (row or {}).get("holder_kind") == "clock"
            )
            if can_retry:
                time.sleep(0.05)
                continue
            _reject_apply(
                "生效临界区正被另一写事务占用，应用未执行",
                policy=policy,
                winner=(row or {}).get("holder") or "unknown",
            )
            raise AssertionError("unreachable")  # pragma: no cover

        recovered = None
        try:
            current = lock_repo.get_lock(conn)
            if current and not lock_repo.is_expired(current):
                kind = current.get("holder_kind") or "unknown"
                winner = current["holder"]
                if kind == "clock" and time.monotonic() < deadline:
                    conn.rollback()
                    time.sleep(0.05)
                    continue
                conn.rollback()
                detail = (
                    "另一个应用批次正在持锁推进，双开应用互斥"
                    if kind == "apply"
                    else "生效临界区已被占用，应用未执行"
                )
                _reject_apply(detail, policy=policy, winner=winner)

            if current and lock_repo.is_expired(current):
                recovered = lock_repo.recover_stale(conn)

            acquired_epoch = time.time()
            fence = lock_repo.acquire(
                conn,
                holder=holder,
                kind="apply",
                ttl_seconds=lock_timeout_seconds(conn),
                at=datetime.fromtimestamp(acquired_epoch, tz=timezone.utc),
            )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if recovered:
            _record_conflict(
                kind="lock_recovered",
                policy=policy,
                winner=holder,
                loser=recovered["holder"],
                detail="锁超时未完成，应用回收后继续",
            )
        return fence, acquired_epoch


def _release_held(holder: str, fence: int) -> None:
    """兜底释放：仅释放仍归自己的锁；已被超时回收时不触碰新持有者。"""
    try:
        conn = connect(timeout=1.0)
        try:
            lock_repo.release(conn, holder, fence)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        # 释放失败时锁会在 expires_at 后失效，下一次获取以新栅栏回收。
        pass


def _signal_fresh(sig: dict[str, Any] | None) -> bool:
    if not sig:
        return False
    requested = float(sig.get("requested_epoch") or 0)
    return time.time() - requested <= config.CLOCK_PREEMPT_WAIT_SECONDS + 30.0


def _open_signal_gate() -> sqlite3.Connection:
    """打开信号库写事务作为提交闸：与应用提交互斥。"""
    signal_repo.ensure_schema()
    deadline = time.monotonic() + config.APPLY_LOCK_WAIT_SECONDS
    while True:
        try:
            return signal_repo.connect(immediate=True, timeout=config.CLOCK_LOCK_ACQUIRE_TIMEOUT)
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def apply_due(hold_ms: int | None = None, line_ms: int | None = None) -> dict[str, Any]:
    """应用所有到期改价单（全局唯一让路策略）。

    临界区：读到期集合 → 写价签/历史 → 更新单状态 → 落批次，
    全部在同一个 SQLite 写事务内，同成同败；锁的释放与批次完成同事务提交。
    """
    hold_ms = config.APPLY_DEMO_HOLD_MS if hold_ms is None else max(int(hold_ms), 0)
    line_ms = config.APPLY_DEMO_LINE_MS if line_ms is None else max(int(line_ms), 0)

    policy = _current_policy()
    holder = _new_holder("apply")
    fence, acquired_epoch = _acquire_apply_lock(holder, policy)
    try:
        if hold_ms:
            # 演示/测试：在临界区内停留，用于复现超时恢复与拨钟抢占。
            time.sleep(min(hold_ms, 60_000) / 1000.0)
        return _run_apply(
            holder=holder,
            fence=fence,
            policy=policy,
            line_ms=line_ms,
        )
    finally:
        # 成功时锁已随批次提交原子释放；中止/异常时这里兜底释放。
        _release_held(holder, fence)


def _run_apply(
    *,
    holder: str,
    fence: int,
    policy: str,
    line_ms: int,
) -> dict[str, Any]:
    written_lines = 0
    try:
        conn = connect(immediate=True, timeout=config.APPLY_LOCK_WAIT_SECONDS)
    except sqlite3.OperationalError:
        # 拨钟的短事务正占着写锁；本次应用未写入任何数据。
        _reject_apply(
            "进入写入阶段时生效临界区被占用，应用未执行",
            policy=policy,
            winner="unknown",
        )
        raise AssertionError("unreachable")  # pragma: no cover
    try:
        # 闸 1：锁仍归本批所有（未被超时回收）。
        if not lock_repo.holds(conn, holder, fence):
            current = lock_repo.get_lock(conn)
            raise _AbortBatch(
                "持锁超时且锁已被回收，本批中止",
                winner=(current or {}).get("holder") or "unknown",
            )

        # 拨钟优先：进入写入前先看一次中止信号。
        if _preempted(policy):
            raise _AbortBatch("收到拨钟，应用中止", winner="clock")

        clock = settings_repo.get(conn, "business_clock")
            ui_clock = settings_repo.get(conn, "ui_clock") or clock
            stamp_clock = ui_clock if ui_clock != clock else clock
        if not clock:
            raise HTTPException(400, "业务钟未设置")

        due = ticket_repo.due_tickets(conn, clock)
        if not due:
            lock_repo.release(conn, holder, fence)
            conn.commit()
            return {
                "applied": False,
                "message": "当前业务钟下没有到期改价单",
                "business_clock": clock,
                "ticket_count": 0,
                "line_count": 0,
                "policy": policy,
                "holder": holder,
                "fence": fence,
            }

        batch_no = batch_repo.next_batch_no(conn)
        created = _now()
        line_total = 0
        applied_tickets: list[str] = []
        batch_id = batch_repo.create_batch(
            conn,
            batch_no=batch_no,
            clock_at=stamp_clock,
            ticket_count=0,
            line_count=0,
            status="running",
            created_at=created,
        )

        for t in due:
            lines = ticket_repo.list_lines(conn, t["id"])
            for line in lines:
                price_repo.upsert_book(
                    conn,
                    sku_code=line["sku_code"],
                    price=float(line["new_price"]),
                    ticket_no=t["ticket_no"],
                    effective_at=t["effective_at"],
                    updated_at=created,
                )
                line_total += 1
                written_lines += 1
                if line_ms:
                    time.sleep(min(line_ms, 5_000) / 1000.0)
                # 拨钟优先：每条明细之间都检查中止信号。
                if _preempted(policy):
                    raise _AbortBatch(
                        "收到拨钟，应用中止并回滚本批",
                        winner="clock",
                        written_lines=written_lines,
                    )
            ticket_repo.mark_applied(conn, t["id"], applied_at=created, batch_id=batch_id)
            applied_tickets.append(t["ticket_no"])
            if _preempted(policy):
                raise _AbortBatch(
                    "收到拨钟，应用中止并回滚本批",
                    winner="clock",
                    written_lines=written_lines,
                )

        # 闸 2：写下批次完成前再复核锁归属，防止超时回收后仍提交。
        if not lock_repo.holds(conn, holder, fence):
            current = lock_repo.get_lock(conn)
            raise _AbortBatch(
                "提交前发现锁已被回收，本批中止",
                winner=(current or {}).get("holder") or "unknown",
                written_lines=written_lines,
            )

        def finish_and_commit() -> None:
            conn.execute(
                "UPDATE apply_batches SET ticket_count=?, line_count=?, status='done' WHERE id=?",
                (len(due), line_total, batch_id),
            )
            # 锁释放与批次完成同处一个事务：外部看到 done 时锁已空闲。
            lock_repo.release(conn, holder, fence)
            conn.commit()

        if policy == "clock_first":
            # 闸 3：在信号库写事务内「最后检查信号 → 提交主库」。
            # 拨钟登记信号也要拿同一写事务，因此信号要么在此之前落库（本批
            # 此刻就能看到并中止），要么只能落在本批提交之后（视为应用已完成，
            # 不再改写已提交批次）。不存在「检查通过、commit 后信号才到」的空窗。
            try:
                sig_conn = _open_signal_gate()
            except sqlite3.OperationalError:
                raise _AbortBatch(
                    "提交闸被占用，未能在拨钟生效前完成本批",
                    winner="clock",
                    written_lines=written_lines,
                ) from None
            try:
                sig = signal_repo.read_preempt(sig_conn)
                if sig and _signal_fresh(sig):
                    raise _AbortBatch(
                        "提交前收到拨钟，应用中止并回滚本批",
                        winner="clock",
                        written_lines=written_lines,
                    )
                finish_and_commit()
                sig_conn.commit()
            finally:
                sig_conn.close()
        else:
            finish_and_commit()

        return {
            "applied": True,
            "batch_no": batch_no,
            "business_clock": clock,
            "ticket_count": len(due),
            "line_count": line_total,
            "tickets": applied_tickets,
            "policy": policy,
            "holder": holder,
            "fence": fence,
        }
    except _AbortBatch as aborted:
        conn.rollback()
        _record_abort(
            holder=holder,
            fence=fence,
            reason=aborted.reason,
            policy=policy,
            written_lines=aborted.written_lines,
        )
        _record_conflict(
            kind="apply_aborted",
            policy=policy,
            winner=aborted.winner,
            loser="apply",
            detail=aborted.reason,
        )
        raise HTTPException(
            409,
            f"{config.POLICY_LABELS[policy]}：{aborted.reason}；"
            "本批已整体回滚，未留下半截 applied",
        ) from None
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
