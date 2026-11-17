from __future__ import annotations

import sqlite3

from app.db import connect
from app.repositories import settings as settings_repo

# 业务钟只有**一个**生效键。拨钟入口、到期扫描、批次戳记、对账钉扎、页面展示
# 全部读这一个键，任何一个环节都不得再引入「另一个当前钟」。
#
# ui_clock 只是历史遗留的展示键：旧版本曾把它当独立钟来拨，导致「页面显示 T2、
# 扫描仍按 T1、批次又写成 T2」的分叉。现在它只在同一事务里随业务钟镜像写入，
# 任何判定都不读它；发现遗留分叉时以 business_clock 为准做一次对齐修复。
CLOCK_KEY = "business_clock"
LEGACY_DISPLAY_KEY = "ui_clock"


def read_clock(conn: sqlite3.Connection) -> str | None:
    """当前生效业务钟（唯一判定来源）。调用方可传入自己的事务连接。"""
    return settings_repo.get(conn, CLOCK_KEY)


def read_clock_standalone() -> str | None:
    conn = connect()
    try:
        return read_clock(conn)
    finally:
        conn.close()


def read_scan_clock() -> str | None:
    """到期扫描使用的钟：与拨钟入口同一个设置键。"""
    return read_clock_standalone()


def read_display_clock() -> str | None:
    """页面/对账展示的钟：同样只读业务钟，不再回退到 ui_clock。"""
    return read_clock_standalone()


def read_legacy_display_key(conn: sqlite3.Connection) -> str | None:
    """仅供分叉审计：遗留展示键的原始值，不参与任何判定。"""
    return settings_repo.get(conn, LEGACY_DISPLAY_KEY)


def write_clock(conn: sqlite3.Connection, clock_at: str) -> None:
    """在调用方事务内落业务钟；遗留展示键同事务镜像，二者不可能分叉。"""
    settings_repo.set_value(conn, CLOCK_KEY, clock_at)
    settings_repo.set_value(conn, LEGACY_DISPLAY_KEY, clock_at)
