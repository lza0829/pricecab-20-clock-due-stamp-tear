from __future__ import annotations

import sqlite3
from typing import Any

from app import config
from app.repositories import settings as settings_repo


POLICY_KEY = "apply_conflict_policy"


def get(conn: sqlite3.Connection) -> dict[str, Any]:
    """当前全局生效的让路策略（唯一）。未显式设置时取环境默认值。"""
    value = settings_repo.get(conn, POLICY_KEY) or config.DEFAULT_CONFLICT_POLICY
    if value not in config.ALLOWED_POLICIES:
        value = config.DEFAULT_CONFLICT_POLICY
    return {
        "policy": value,
        "policy_label": config.POLICY_LABELS.get(value, value),
        "default_policy": config.DEFAULT_CONFLICT_POLICY,
        "allowed": list(config.ALLOWED_POLICIES),
        "lock_timeout_seconds": config.APPLY_LOCK_TIMEOUT_SECONDS,
        "lock_wait_seconds": config.APPLY_LOCK_WAIT_SECONDS,
    }


def set_value(conn: sqlite3.Connection, policy: str) -> dict[str, Any]:
    """切换全局策略。一次只能有一个生效值，写入即替换。"""
    policy = (policy or "").strip().lower()
    if policy not in config.ALLOWED_POLICIES:
        raise ValueError(f"未知让路策略 {policy!r}")
    settings_repo.set_value(conn, POLICY_KEY, policy)
    return get(conn)
