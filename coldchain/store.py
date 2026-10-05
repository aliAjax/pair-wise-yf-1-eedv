"""SQLite 存储层：连接管理、事务、建表。

并发要点：
- 单连接 + RLock 串行化，所有写操作包在 BEGIN IMMEDIATE 事务里；
- batch_occupancy 上的部分唯一索引是"后到者先确认占用"的最终防线，
  即使绕过 service 直接写库也无法出现两个未释放的占用。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id            TEXT PRIMARY KEY,
    name                    TEXT NOT NULL DEFAULT '',
    default_batch_capacity  INTEGER NOT NULL DEFAULT 1000
);

-- 批次库存（按 批次号+仓库 唯一定位；账面余量、冻结、在途、对账挂账分列）
CREATE TABLE IF NOT EXISTS batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no            TEXT NOT NULL,
    drug_code           TEXT NOT NULL,
    warehouse_id        TEXT NOT NULL,
    total_qty           INTEGER NOT NULL DEFAULT 0,   -- 账面余量
    frozen_qty          INTEGER NOT NULL DEFAULT 0,   -- 温控冻结量
    in_transit_qty      INTEGER NOT NULL DEFAULT 0,   -- 在途量（已发出未到货）
    hold_qty            INTEGER NOT NULL DEFAULT 0,   -- 对账挂账（余量不动但不可放）
    available_qty       INTEGER NOT NULL DEFAULT 0,   -- 可放量（重算后落库）
    capacity            INTEGER NOT NULL,
    temp_min            REAL NOT NULL DEFAULT 2.0,
    temp_max            REAL NOT NULL DEFAULT 8.0,
    temp_violated       INTEGER NOT NULL DEFAULT 0,   -- 是否曾温度超限（历史事实，不随解冻清除）
    opening_backfilled  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    UNIQUE(batch_no, warehouse_id)
);

CREATE TABLE IF NOT EXISTS transfer_orders (
    order_no        TEXT PRIMARY KEY,
    batch_no        TEXT NOT NULL,
    from_warehouse  TEXT NOT NULL,
    to_warehouse    TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    status          TEXT NOT NULL,
    shipped_qty     INTEGER,
    received_qty    INTEGER,
    temp_excursion  INTEGER NOT NULL DEFAULT 0,  -- 在途是否发生温度超限
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_dest ON transfer_orders(batch_no, to_warehouse, status);

-- 批次占用：同一批次同一发出仓同一时刻只允许一个未释放占用
CREATE TABLE IF NOT EXISTS batch_occupancy (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT NOT NULL,
    warehouse_id  TEXT NOT NULL,
    order_no      TEXT NOT NULL,
    acquired_at   TEXT NOT NULL,
    released_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_occupancy
    ON batch_occupancy(batch_no, warehouse_id) WHERE released_at IS NULL;

-- 车厢上报记录：report_id 唯一，重复上报只入库一次
CREATE TABLE IF NOT EXISTS transport_reports (
    report_id    TEXT PRIMARY KEY,
    order_no     TEXT NOT NULL,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temperature_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT NOT NULL,
    warehouse_id  TEXT,          -- 在途上报时为空
    order_no      TEXT,
    temp          REAL NOT NULL,
    source        TEXT NOT NULL, -- WAREHOUSE / TRUCK
    recorded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT NOT NULL,
    batch_no        TEXT NOT NULL,
    from_warehouse  TEXT NOT NULL,
    to_warehouse    TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    reason          TEXT NOT NULL,   -- SHORTAGE：到货少于发出
    status          TEXT NOT NULL,
    resolution      TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS stock_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT NOT NULL,
    warehouse_id  TEXT NOT NULL,
    change_type   TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    ref_order     TEXT,
    note          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_batch ON stock_ledger(batch_no, warehouse_id);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """写事务：BEGIN IMMEDIATE 保证检查-写入原子性，异常自动回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
