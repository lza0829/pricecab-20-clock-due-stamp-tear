from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.db import connect


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_schema(conn) -> None:
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS skus (
          id INTEGER PRIMARY KEY,
          sku_code TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          category TEXT NOT NULL,
          list_price REAL NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS price_tickets (
          id INTEGER PRIMARY KEY,
          ticket_no TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          status TEXT NOT NULL,
          effective_at TEXT NOT NULL,
          note TEXT,
          created_at TEXT NOT NULL,
          applied_at TEXT,
          apply_batch_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS price_ticket_lines (
          id INTEGER PRIMARY KEY,
          ticket_id INTEGER NOT NULL REFERENCES price_tickets(id) ON DELETE CASCADE,
          sku_code TEXT NOT NULL,
          old_price REAL NOT NULL,
          new_price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS apply_batches (
          id INTEGER PRIMARY KEY,
          batch_no TEXT NOT NULL UNIQUE,
          clock_at TEXT NOT NULL,
          ticket_count INTEGER NOT NULL,
          line_count INTEGER NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_book (
          sku_code TEXT PRIMARY KEY,
          current_price REAL NOT NULL,
          source_ticket_no TEXT,
          effective_at TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_history (
          id INTEGER PRIMARY KEY,
          sku_code TEXT NOT NULL,
          price REAL NOT NULL,
          source_ticket_no TEXT,
          effective_at TEXT NOT NULL,
          recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sales_fixture (
          id INTEGER PRIMARY KEY,
          sale_no TEXT NOT NULL UNIQUE,
          sku_code TEXT NOT NULL,
          sold_at TEXT NOT NULL,
          unit_price REAL NOT NULL,
          qty INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hold_applies (
          id INTEGER PRIMARY KEY,
          ticket_id INTEGER,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS apply_lock (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          holder TEXT NOT NULL DEFAULT '',
          holder_kind TEXT NOT NULL DEFAULT '',
          acquired_at TEXT NOT NULL DEFAULT '',
          expires_at TEXT NOT NULL DEFAULT '',
          acquired_epoch REAL NOT NULL DEFAULT 0,
          expires_epoch REAL NOT NULL DEFAULT 0,
          fence INTEGER NOT NULL DEFAULT 0,
          released_at TEXT
        );
        INSERT OR IGNORE INTO apply_lock(id) VALUES (1);
        '''
    )
    _ensure_apply_batch_columns(conn)


# 批次必须同时记住「扫描用的钟」与「戳记的钟」，二者由同一设置键读出。
# 老库补列；不回溯填 scan_clock：老批次是否戳记错误，交给分叉审计按
# 「批次内单的生效时刻 / 同戳记下应扫未扫的单」互证，不能用当前钟倒填掩盖。
_APPLY_BATCH_COLUMNS = (
    ("scan_clock", "TEXT"),
    ("voided_at", "TEXT"),
    ("void_reason", "TEXT"),
)


def _ensure_apply_batch_columns(conn) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(apply_batches)")}
    for name, ddl in _APPLY_BATCH_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE apply_batches ADD COLUMN {name} {ddl}")


def seed_if_empty() -> None:
    conn = connect()
    try:
        init_schema(conn)
        n = conn.execute("SELECT COUNT(*) c FROM skus").fetchone()["c"]
        if n:
            conn.commit()
            return

        skus = json.loads((FIXTURES / "skus.json").read_text(encoding="utf-8"))
        sales = json.loads((FIXTURES / "sales.json").read_text(encoding="utf-8"))
        now = _now()
        for s in skus:
            conn.execute(
                "INSERT INTO skus(sku_code, title, category, list_price, enabled) VALUES (?,?,?,?,1)",
                (s["sku_code"], s["title"], s["category"], float(s["list_price"])),
            )
            conn.execute(
                "INSERT INTO price_book(sku_code, current_price, source_ticket_no, effective_at, updated_at) VALUES (?,?,NULL,?,?)",
                (s["sku_code"], float(s["list_price"]), now, now),
            )
            conn.execute(
                "INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at) VALUES (?,?,NULL,?,?)",
                (s["sku_code"], float(s["list_price"]), now, now),
            )
        for row in sales:
            conn.execute(
                "INSERT INTO sales_fixture(sale_no, sku_code, sold_at, unit_price, qty) VALUES (?,?,?,?,?)",
                (row["sale_no"], row["sku_code"], row["sold_at"], float(row["unit_price"]), int(row["qty"])),
            )
        # 业务钟只有一个生效键；ui_clock 只作遗留兼容镜像，判定一律读 business_clock。
        for key in ("business_clock", "ui_clock"):
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (key, "2026-11-16T09:00:00Z"),
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('clock_label', ?)",
            ("业务钟（可拨，用于判断改价是否到期）",),
        )
        conn.commit()
    finally:
        conn.close()
