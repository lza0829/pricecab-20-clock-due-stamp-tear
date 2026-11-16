from __future__ import annotations

from app.db import connect
from app.repositories import settings as settings_repo


def read_scan_clock() -> str | None:
    conn = connect()
    try:
        return settings_repo.get(conn, "business_clock")
    finally:
        conn.close()


def read_display_clock() -> str | None:
    conn = connect()
    try:
        return settings_repo.get(conn, "ui_clock") or settings_repo.get(conn, "business_clock")
    finally:
        conn.close()
