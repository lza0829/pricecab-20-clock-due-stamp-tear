from __future__ import annotations

from typing import Any

from app.db import connect
from app.repositories import settings as settings_repo
from app.services import clock_plane


def snapshot_clocks() -> dict[str, Any]:
    conn = connect()
    try:
        business = settings_repo.get(conn, "business_clock")
        ui = settings_repo.get(conn, "ui_clock")
        return {
            "business_clock": business,
            "ui_clock": ui,
            "scan_clock": clock_plane.read_scan_clock(),
            "display_clock": clock_plane.read_display_clock(),
            "aligned": business == ui,
        }
    finally:
        conn.close()


def advance_display_only(iso: str) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, "ui_clock", iso)
        conn.commit()
        return snapshot_clocks()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
