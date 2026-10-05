"""调拨核对业务逻辑。

关键语义：
- 可放量 available = 账面余量 total - 冻结 frozen - 在途 in_transit - 对账挂账 hold，
  每次相关变动后重算并落库（温控变化必触发重算）。
- 批次占用键 = (批次号, 发出仓)：同一物理库存的并发调拨互斥；不同仓的同批次号
  物理库存独立，互不阻塞。占用随 QUEUED/IN_TRANSIT 单持有，到货或取消时释放。
- 到货短缺：源仓账面只减实收量，短缺部分进 hold_qty 并挂跨仓对账待处理，
  批次余量不跟着改；对账核销时才动账面。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .models import (
    ApiError,
    BatchOccupiedError,
    EventType,
    LedgerType,
    OrderStatus,
    ReconResolution,
    ReconStatus,
)
from .store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_batch(row: dict) -> dict:
    return {
        "batch_no": row["batch_no"],
        "drug_code": row["drug_code"],
        "warehouse_id": row["warehouse_id"],
        "total_qty": row["total_qty"],
        "frozen_qty": row["frozen_qty"],
        "in_transit_qty": row["in_transit_qty"],
        "hold_qty": row["hold_qty"],
        "available_qty": row["available_qty"],
        "capacity": row["capacity"],
        "temp_min": row["temp_min"],
        "temp_max": row["temp_max"],
        "temp_violated": bool(row["temp_violated"]),
        "opening_backfilled": bool(row["opening_backfilled"]),
    }


def _public_order(row: dict) -> dict:
    return {
        "order_no": row["order_no"],
        "batch_no": row["batch_no"],
        "from_warehouse": row["from_warehouse"],
        "to_warehouse": row["to_warehouse"],
        "qty": row["qty"],
        "status": row["status"],
        "shipped_qty": row["shipped_qty"],
        "received_qty": row["received_qty"],
        "temp_excursion": bool(row["temp_excursion"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class ColdChainService:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # 基础档案
    # ------------------------------------------------------------------

    def register_warehouse(self, warehouse_id: str, name: str = "",
                           default_batch_capacity: int = 1000) -> dict:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO warehouses(warehouse_id, name, default_batch_capacity)"
                " VALUES (?,?,?)",
                (warehouse_id, name, default_batch_capacity),
            )
        return {"warehouse_id": warehouse_id, "name": name,
                "default_batch_capacity": default_batch_capacity}

    def register_batch(self, batch_no: str, drug_code: str, warehouse_id: str,
                       qty: int, capacity: int | None = None,
                       temp_min: float = 2.0, temp_max: float = 8.0,
                       record_opening: bool = True) -> dict:
        """登记批次库存。record_opening=False 用于导入旧系统数据（无流水，待回填期初）。"""
        if qty < 0:
            raise ApiError(422, "INVALID_QTY", "期初数量不能为负")
        with self.store.transaction() as conn:
            wh = self._must_warehouse(conn, warehouse_id)
            cap = capacity if capacity is not None else wh["default_batch_capacity"]
            try:
                conn.execute(
                    "INSERT INTO batches(batch_no, drug_code, warehouse_id, total_qty, capacity,"
                    "                   temp_min, temp_max, available_qty, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (batch_no, drug_code, warehouse_id, qty, cap, temp_min, temp_max, qty, _now()),
                )
            except sqlite3.IntegrityError:
                raise ApiError(409, "BATCH_EXISTS",
                               f"批次 {batch_no} 在仓库 {warehouse_id} 已存在")
            if record_opening and qty > 0:
                self._ledger(conn, batch_no, warehouse_id, LedgerType.OPENING, qty,
                             None, "新批次登记期初")
            self._recalc(conn, batch_no, warehouse_id)
            return _public_batch(self._must_batch(conn, batch_no, warehouse_id))

    # ------------------------------------------------------------------
    # 调拨单
    # ------------------------------------------------------------------

    def submit_transfer(self, order_no: str, batch_no: str, from_warehouse: str,
                        to_warehouse: str, qty: int) -> tuple[dict, bool]:
        """提交调拨单。返回 (订单, 是否新建)。

        流程：幂等检查 → 确认批次占用（后到者未释放即挡回并告知当前单号）
        → 校验可放量 → 目的仓容量满则排队 → 否则发出（在途增加、可放重算）。
        """
        if from_warehouse == to_warehouse:
            raise ApiError(422, "SAME_WAREHOUSE", "调出仓与调入仓不能相同")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ApiError(422, "INVALID_QTY", "调拨数量必须为正整数")
        with self.store.transaction() as conn:
            existing = self._find_order(conn, order_no)
            if existing:
                # 幂等重提：参数必须一致，否则视为单号冲突
                same = (existing["batch_no"] == batch_no
                        and existing["from_warehouse"] == from_warehouse
                        and existing["to_warehouse"] == to_warehouse
                        and existing["qty"] == qty)
                if not same:
                    raise ApiError(409, "ORDER_NO_CONFLICT",
                                   f"单号 {order_no} 已存在且参数不一致")
                return _public_order(existing), False

            batch = self._must_batch(conn, batch_no, from_warehouse)
            self._must_warehouse(conn, to_warehouse)

            # 关键：后到的请求先确认批次占用；占用未释放则挡回并告知当前单号。
            # 占用冲突/可放不足等异常会回滚整个事务，本次占用不会残留。
            self._acquire_occupancy(conn, batch_no, from_warehouse, order_no)

            self._recalc(conn, batch_no, from_warehouse)
            batch = self._must_batch(conn, batch_no, from_warehouse)
            if qty > batch["available_qty"]:
                raise ApiError(
                    422, "INSUFFICIENT_AVAILABLE",
                    f"批次 {batch_no} 可放量不足: 需 {qty}, 当前可放 {batch['available_qty']}",
                    available_qty=batch["available_qty"])

            dest = self._ensure_dest_batch(conn, batch, to_warehouse)
            # 容量检查把在途和排队中的量都算作已预留，保证后到的小单不会插队
            incoming = self._incoming_qty(conn, batch_no, to_warehouse)
            queued = self._queued_qty(conn, batch_no, to_warehouse)
            if dest["total_qty"] + incoming + queued + qty > dest["capacity"]:
                # 批次容量满了 → 排队（保留占用，容量腾出后按序放行）
                order = self._create_order(conn, order_no, batch_no, from_warehouse,
                                           to_warehouse, qty, OrderStatus.QUEUED)
                return _public_order(order), True

            conn.execute("UPDATE batches SET in_transit_qty = in_transit_qty + ? WHERE id = ?",
                         (qty, batch["id"]))
            self._ledger(conn, batch_no, from_warehouse, LedgerType.TRANSFER_OUT, qty,
                         order_no, "调拨发出")
            order = self._create_order(conn, order_no, batch_no, from_warehouse,
                                       to_warehouse, qty, OrderStatus.IN_TRANSIT,
                                       shipped_qty=qty)
            self._recalc(conn, batch_no, from_warehouse)
            return _public_order(order), True

    def cancel_transfer(self, order_no: str) -> dict:
        """取消排队中的调拨单并释放占用；在途单不能取消（货已离库，需走到货流程）。"""
        with self.store.transaction() as conn:
            order = self._must_order(conn, order_no)
            if order["status"] != OrderStatus.QUEUED.value:
                raise ApiError(409, "ORDER_NOT_CANCELLABLE",
                               f"订单状态 {order['status']} 不可取消")
            conn.execute("UPDATE transfer_orders SET status = ?, updated_at = ? WHERE order_no = ?",
                         (OrderStatus.CANCELLED.value, _now(), order_no))
            self._release_occupancy(conn, order["batch_no"], order["from_warehouse"], order_no)
            return _public_order(self._must_order(conn, order_no))

    def get_order(self, order_no: str) -> dict:
        row = self.store.one("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,))
        if not row:
            raise ApiError(404, "ORDER_NOT_FOUND", f"调拨单 {order_no} 不存在")
        return _public_order(row)

    def list_queue(self, batch_no: str | None = None,
                   warehouse_id: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM transfer_orders WHERE status = ?"
               " AND (? IS NULL OR batch_no = ?)"
               " AND (? IS NULL OR to_warehouse = ?)"
               " ORDER BY created_at, order_no")
        rows = self.store.all(sql, (OrderStatus.QUEUED.value,
                                    batch_no, batch_no, warehouse_id, warehouse_id))
        return [_public_order(r) for r in rows]

    # ------------------------------------------------------------------
    # 运输上报合并（车厢断网恢复后批量补报）
    # ------------------------------------------------------------------

    def merge_transport_reports(self, reports: list[dict]) -> dict:
        """合并车厢离线期间缓存的上报。同一 report_id 重复上报只入库一次。

        每条记录独立事务：单条出错不影响其余记录合并。
        """
        results = []
        for rep in reports:
            report_id = rep.get("report_id")
            order_no = rep.get("order_no")
            if not report_id or not order_no:
                results.append({"report_id": report_id, "order_no": order_no,
                                "result": "ERROR", "code": "MISSING_FIELD",
                                "message": "缺少 report_id 或 order_no"})
                continue
            try:
                with self.store.transaction() as conn:
                    dup = conn.execute(
                        "SELECT report_id FROM transport_reports WHERE report_id = ?",
                        (report_id,)).fetchone()
                    if dup:
                        results.append({"report_id": report_id, "order_no": order_no,
                                        "result": "DUPLICATE",
                                        "message": "重复上报，已忽略（只入库一次）"})
                        continue
                    self._must_order(conn, order_no)
                    conn.execute(
                        "INSERT INTO transport_reports(report_id, order_no, payload, received_at)"
                        " VALUES (?,?,?,?)",
                        (report_id, order_no, json.dumps(rep, ensure_ascii=False), _now()))
                    for event in rep.get("events", []):
                        self._apply_transport_event(conn, order_no, event)
                    order = self._must_order(conn, order_no)
                    results.append({"report_id": report_id, "order_no": order_no,
                                    "result": "MERGED", "order_status": order["status"]})
            except ApiError as e:
                results.append({"report_id": report_id, "order_no": order_no,
                                "result": "ERROR", "code": e.code, "message": e.message})
        return {"results": results}

    def _apply_transport_event(self, conn, order_no: str, event: dict) -> None:
        etype = event.get("type")
        if etype == EventType.DEPARTED.value:
            return  # 发出状态在提交调拨单时已建立，出发事件仅作记录留痕
        if etype == EventType.TEMP_READING.value:
            temp = event.get("temp")
            if temp is None:
                raise ApiError(422, "MISSING_FIELD", "TEMP_READING 缺少 temp")
            order = self._must_order(conn, order_no)
            conn.execute(
                "INSERT INTO temperature_events(batch_no, warehouse_id, order_no, temp, source,"
                " recorded_at) VALUES (?,?,?,?,?,?)",
                (order["batch_no"], None, order_no, float(temp), "TRUCK", _now()))
            src = self._find_batch(conn, order["batch_no"], order["from_warehouse"])
            if src and not (src["temp_min"] <= float(temp) <= src["temp_max"]):
                # 在途温度超限：标记 excursion，到货时自动冻结入目的仓
                conn.execute(
                    "UPDATE transfer_orders SET temp_excursion = 1, updated_at = ?"
                    " WHERE order_no = ?", (_now(), order_no))
            return
        if etype == EventType.ARRIVED.value:
            received = event.get("received_qty")
            if not isinstance(received, int) or received < 0:
                raise ApiError(422, "MISSING_FIELD", "ARRIVED 缺少合法的 received_qty")
            self._process_arrival(conn, order_no, received)
            return
        raise ApiError(422, "UNKNOWN_EVENT", f"未知事件类型: {etype}")

    def _process_arrival(self, conn, order_no: str, received_qty: int) -> None:
        order = self._must_order(conn, order_no)
        if order["status"] == OrderStatus.COMPLETED.value:
            return  # 幂等：重复到货事件不重复入账
        if order["status"] != OrderStatus.IN_TRANSIT.value:
            raise ApiError(409, "ORDER_NOT_IN_TRANSIT",
                           f"订单 {order_no} 状态 {order['status']}，不能确认到货")
        shipped = order["shipped_qty"]
        if received_qty > shipped:
            raise ApiError(422, "OVER_RECEIVED",
                           f"实收 {received_qty} 超过发出 {shipped}")
        shortage = shipped - received_qty
        batch_no = order["batch_no"]
        src = self._must_batch(conn, batch_no, order["from_warehouse"])
        dst = self._must_batch(conn, batch_no, order["to_warehouse"])

        # 源仓：在途按发出量清零；账面只减实收量；短缺挂 hold，批次余量不跟着改
        conn.execute(
            "UPDATE batches SET in_transit_qty = in_transit_qty - ?,"
            "                   total_qty = total_qty - ?,"
            "                   hold_qty = hold_qty + ?"
            " WHERE id = ?",
            (shipped, received_qty, shortage, src["id"]))

        if order["temp_excursion"]:
            # 温度超限过的库存自动冻结：到货直接进目的仓冻结量
            conn.execute(
                "UPDATE batches SET total_qty = total_qty + ?, frozen_qty = frozen_qty + ?,"
                " temp_violated = 1 WHERE id = ?",
                (received_qty, received_qty, dst["id"]))
            self._ledger(conn, batch_no, dst["warehouse_id"], LedgerType.TRANSFER_IN,
                         received_qty, order_no, "到货（在途温度超限，自动冻结）")
            if received_qty:
                self._ledger(conn, batch_no, dst["warehouse_id"], LedgerType.FREEZE,
                             received_qty, order_no, "在途温度超限自动冻结")
        else:
            conn.execute("UPDATE batches SET total_qty = total_qty + ? WHERE id = ?",
                         (received_qty, dst["id"]))
            self._ledger(conn, batch_no, dst["warehouse_id"], LedgerType.TRANSFER_IN,
                         received_qty, order_no, "调拨到货")

        if shortage > 0:
            # 到货少于发出 → 挂跨仓对账待处理
            conn.execute(
                "INSERT INTO reconciliation_items(order_no, batch_no, from_warehouse,"
                " to_warehouse, qty, reason, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (order_no, batch_no, order["from_warehouse"], order["to_warehouse"],
                 shortage, "SHORTAGE", ReconStatus.PENDING.value, _now()))

        conn.execute(
            "UPDATE transfer_orders SET status = ?, received_qty = ?, updated_at = ?"
            " WHERE order_no = ?",
            (OrderStatus.COMPLETED.value, received_qty, _now(), order_no))
        self._release_occupancy(conn, batch_no, order["from_warehouse"], order_no)
        self._recalc(conn, batch_no, order["from_warehouse"])
        self._recalc(conn, batch_no, order["to_warehouse"])
        # 源仓账面下降腾出容量 → 尝试放行以源仓为目的仓的排队单
        self._process_queue(conn, batch_no, order["from_warehouse"])

    # ------------------------------------------------------------------
    # 温控
    # ------------------------------------------------------------------

    def record_temperature(self, batch_no: str, warehouse_id: str, temp: float,
                           source: str = "WAREHOUSE") -> dict:
        """仓内温度上报：温控变化就重算可放量；超限过的库存自动冻结。"""
        with self.store.transaction() as conn:
            batch = self._must_batch(conn, batch_no, warehouse_id)
            conn.execute(
                "INSERT INTO temperature_events(batch_no, warehouse_id, order_no, temp, source,"
                " recorded_at) VALUES (?,?,?,?,?,?)",
                (batch_no, warehouse_id, None, float(temp), source, _now()))
            if not (batch["temp_min"] <= float(temp) <= batch["temp_max"]):
                self._freeze_batch(conn, batch,
                                   f"温度超限 {temp}℃（允许 {batch['temp_min']}~{batch['temp_max']}℃）")
            self._recalc(conn, batch_no, warehouse_id)
            return _public_batch(self._must_batch(conn, batch_no, warehouse_id))

    def update_temp_range(self, batch_no: str, warehouse_id: str,
                          temp_min: float, temp_max: float) -> dict:
        """温控标准变化：按最近一条仓内温度记录重新评估并重算可放量。"""
        if temp_min >= temp_max:
            raise ApiError(422, "INVALID_RANGE", "temp_min 必须小于 temp_max")
        with self.store.transaction() as conn:
            batch = self._must_batch(conn, batch_no, warehouse_id)
            conn.execute("UPDATE batches SET temp_min = ?, temp_max = ? WHERE id = ?",
                         (float(temp_min), float(temp_max), batch["id"]))
            last = conn.execute(
                "SELECT temp FROM temperature_events"
                " WHERE batch_no = ? AND warehouse_id = ? ORDER BY id DESC LIMIT 1",
                (batch_no, warehouse_id)).fetchone()
            if last and not (float(temp_min) <= last["temp"] <= float(temp_max)):
                batch = self._must_batch(conn, batch_no, warehouse_id)
                self._freeze_batch(conn, batch,
                                   f"温控标准调整为 {temp_min}~{temp_max}℃，"
                                   f"最近温度 {last['temp']}℃ 超限")
            self._recalc(conn, batch_no, warehouse_id)
            return _public_batch(self._must_batch(conn, batch_no, warehouse_id))

    def unfreeze_batch(self, batch_no: str, warehouse_id: str, note: str = "") -> dict:
        """QA 人工解冻（超限冻结不会自动解除，必须人工放行）。"""
        with self.store.transaction() as conn:
            batch = self._must_batch(conn, batch_no, warehouse_id)
            if batch["frozen_qty"] == 0:
                raise ApiError(409, "NOT_FROZEN", "该批次当前无冻结库存")
            self._ledger(conn, batch_no, warehouse_id, LedgerType.UNFREEZE,
                         batch["frozen_qty"], None, note or "QA 人工解冻")
            conn.execute("UPDATE batches SET frozen_qty = 0 WHERE id = ?", (batch["id"],))
            self._recalc(conn, batch_no, warehouse_id)
            return _public_batch(self._must_batch(conn, batch_no, warehouse_id))

    def update_capacity(self, batch_no: str, warehouse_id: str, capacity: int) -> dict:
        """调整批次容量；扩容后尝试放行排队单。"""
        if capacity < 0:
            raise ApiError(422, "INVALID_CAPACITY", "容量不能为负")
        with self.store.transaction() as conn:
            self._must_batch(conn, batch_no, warehouse_id)
            conn.execute("UPDATE batches SET capacity = ? WHERE batch_no = ? AND warehouse_id = ?",
                         (capacity, batch_no, warehouse_id))
            self._process_queue(conn, batch_no, warehouse_id)
            self._recalc(conn, batch_no, warehouse_id)
            return _public_batch(self._must_batch(conn, batch_no, warehouse_id))

    def _freeze_batch(self, conn, batch: dict, reason: str) -> None:
        """冻结该仓该批次全部在库库存；排队中的调出单取消并释放占用。"""
        on_hand = batch["total_qty"] - batch["in_transit_qty"]
        delta = on_hand - batch["frozen_qty"]
        if delta > 0:
            conn.execute("UPDATE batches SET frozen_qty = ?, temp_violated = 1 WHERE id = ?",
                         (on_hand, batch["id"]))
            self._ledger(conn, batch["batch_no"], batch["warehouse_id"], LedgerType.FREEZE,
                         delta, None, reason)
        else:
            conn.execute("UPDATE batches SET temp_violated = 1 WHERE id = ?", (batch["id"],))
        queued = conn.execute(
            "SELECT order_no FROM transfer_orders"
            " WHERE batch_no = ? AND from_warehouse = ? AND status = ?",
            (batch["batch_no"], batch["warehouse_id"], OrderStatus.QUEUED.value)).fetchall()
        for row in queued:
            conn.execute("UPDATE transfer_orders SET status = ?, updated_at = ?"
                         " WHERE order_no = ?",
                         (OrderStatus.CANCELLED.value, _now(), row["order_no"]))
            self._release_occupancy(conn, batch["batch_no"], batch["warehouse_id"],
                                    row["order_no"])

    # ------------------------------------------------------------------
    # 跨仓对账
    # ------------------------------------------------------------------

    def list_reconciliation(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM reconciliation_items WHERE (? IS NULL OR status = ?)"
               " ORDER BY id")
        return self.store.all(sql, (status, status))

    def resolve_reconciliation(self, item_id: int, resolution: str) -> dict:
        """核销对账挂账：直到这里才动批次账面余量。"""
        try:
            res = ReconResolution(resolution)
        except ValueError:
            raise ApiError(422, "INVALID_RESOLUTION",
                           f"resolution 须为 {[r.value for r in ReconResolution]}")
        with self.store.transaction() as conn:
            item = conn.execute("SELECT * FROM reconciliation_items WHERE id = ?",
                                (item_id,)).fetchone()
            if not item:
                raise ApiError(404, "RECON_NOT_FOUND", f"对账单 {item_id} 不存在")
            if item["status"] != ReconStatus.PENDING.value:
                raise ApiError(409, "RECON_ALREADY_RESOLVED", "该对账单已处理")
            qty = item["qty"]
            batch_no = item["batch_no"]
            src = self._must_batch(conn, batch_no, item["from_warehouse"])
            dst = self._must_batch(conn, batch_no, item["to_warehouse"])

            if res is ReconResolution.WRITE_OFF:
                # 确认损耗：核减源仓账面与挂账
                conn.execute("UPDATE batches SET total_qty = total_qty - ?,"
                             " hold_qty = hold_qty - ? WHERE id = ?", (qty, qty, src["id"]))
                self._ledger(conn, batch_no, src["warehouse_id"],
                             LedgerType.RECON_WRITE_OFF, qty, item["order_no"],
                             "对账确认损耗，核减账面")
            elif res is ReconResolution.RELEASE_TO_SOURCE:
                # 货物找回/实际未发出：解除挂账，余量不动
                conn.execute("UPDATE batches SET hold_qty = hold_qty - ? WHERE id = ?",
                             (qty, src["id"]))
                self._ledger(conn, batch_no, src["warehouse_id"],
                             LedgerType.RECON_RELEASE, qty, item["order_no"],
                             "对账解除挂账")
            else:  # DELIVER_TO_DEST 补记到货
                conn.execute("UPDATE batches SET total_qty = total_qty - ?,"
                             " hold_qty = hold_qty - ? WHERE id = ?", (qty, qty, src["id"]))
                conn.execute("UPDATE batches SET total_qty = total_qty + ? WHERE id = ?",
                             (qty, dst["id"]))
                self._ledger(conn, batch_no, src["warehouse_id"],
                             LedgerType.RECON_DELIVER, qty, item["order_no"],
                             "对账补记出库")
                self._ledger(conn, batch_no, dst["warehouse_id"],
                             LedgerType.RECON_DELIVER, qty, item["order_no"],
                             "对账补记到货")

            conn.execute("UPDATE reconciliation_items SET status = ?, resolution = ?,"
                         " resolved_at = ? WHERE id = ?",
                         (ReconStatus.RESOLVED.value, res.value, _now(), item_id))
            self._recalc(conn, batch_no, src["warehouse_id"])
            self._recalc(conn, batch_no, dst["warehouse_id"])
            # 源仓账面变化可能腾出容量
            self._process_queue(conn, batch_no, src["warehouse_id"])
            return self.store.one("SELECT * FROM reconciliation_items WHERE id = ?",
                                  (item_id,))

    # ------------------------------------------------------------------
    # 旧数据回填期初
    # ------------------------------------------------------------------

    def backfill_opening(self) -> dict:
        """旧数据缺调拨/流水记录的批次，按现存数量回填期初（幂等）。"""
        backfilled = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT b.* FROM batches b"
                " WHERE b.opening_backfilled = 0 AND b.total_qty > 0"
                "   AND NOT EXISTS (SELECT 1 FROM stock_ledger l"
                "                   WHERE l.batch_no = b.batch_no"
                "                     AND l.warehouse_id = b.warehouse_id)"
            ).fetchall()
            for b in rows:
                self._ledger(conn, b["batch_no"], b["warehouse_id"],
                             LedgerType.OPENING_BACKFILL, b["total_qty"], None,
                             "旧数据缺调拨记录，按现存数量回填期初")
                conn.execute("UPDATE batches SET opening_backfilled = 1 WHERE id = ?",
                             (b["id"],))
                backfilled.append({"batch_no": b["batch_no"],
                                   "warehouse_id": b["warehouse_id"],
                                   "qty": b["total_qty"]})
        return {"backfilled": backfilled, "count": len(backfilled)}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_batch(self, batch_no: str, warehouse_id: str) -> dict:
        with self.store.transaction() as conn:
            self._recalc(conn, batch_no, warehouse_id)  # 保证可放量是最新重算结果
            return _public_batch(self._must_batch(conn, batch_no, warehouse_id))

    def list_ledger(self, batch_no: str | None = None,
                    warehouse_id: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM stock_ledger WHERE (? IS NULL OR batch_no = ?)"
               " AND (? IS NULL OR warehouse_id = ?) ORDER BY id")
        return self.store.all(sql, (batch_no, batch_no, warehouse_id, warehouse_id))

    # ------------------------------------------------------------------
    # 内部：占用 / 排队 / 重算
    # ------------------------------------------------------------------

    def _acquire_occupancy(self, conn, batch_no: str, warehouse_id: str,
                           order_no: str) -> None:
        try:
            conn.execute(
                "INSERT INTO batch_occupancy(batch_no, warehouse_id, order_no, acquired_at)"
                " VALUES (?,?,?,?)",
                (batch_no, warehouse_id, order_no, _now()))
        except sqlite3.IntegrityError:
            holder = conn.execute(
                "SELECT order_no FROM batch_occupancy"
                " WHERE batch_no = ? AND warehouse_id = ? AND released_at IS NULL",
                (batch_no, warehouse_id)).fetchone()
            raise BatchOccupiedError(holder["order_no"] if holder else "UNKNOWN")

    def _release_occupancy(self, conn, batch_no: str, warehouse_id: str,
                           order_no: str) -> None:
        conn.execute(
            "UPDATE batch_occupancy SET released_at = ?"
            " WHERE batch_no = ? AND warehouse_id = ? AND order_no = ?"
            "   AND released_at IS NULL",
            (_now(), batch_no, warehouse_id, order_no))

    def _process_queue(self, conn, batch_no: str, warehouse_id: str) -> None:
        """容量腾出后按提交顺序放行排队单（严格 FIFO，队头放不下就停）。"""
        dest = self._find_batch(conn, batch_no, warehouse_id)
        if not dest:
            return
        queued = conn.execute(
            "SELECT * FROM transfer_orders WHERE batch_no = ? AND to_warehouse = ?"
            " AND status = ? ORDER BY created_at, order_no",
            (batch_no, warehouse_id, OrderStatus.QUEUED.value)).fetchall()
        for order in queued:
            room = (dest["capacity"] - dest["total_qty"]
                    - self._incoming_qty(conn, batch_no, warehouse_id))
            if room < order["qty"]:
                break
            src = self._find_batch(conn, batch_no, order["from_warehouse"])
            if src is None:
                conn.execute("UPDATE transfer_orders SET status = ?, updated_at = ?"
                             " WHERE order_no = ?",
                             (OrderStatus.CANCELLED.value, _now(), order["order_no"]))
                self._release_occupancy(conn, batch_no, order["from_warehouse"],
                                        order["order_no"])
                continue
            available = (src["total_qty"] - src["frozen_qty"] - src["in_transit_qty"]
                         - src["hold_qty"])
            if available < order["qty"]:
                break  # 源仓可放不足，留在队列等下次
            conn.execute("UPDATE batches SET in_transit_qty = in_transit_qty + ? WHERE id = ?",
                         (order["qty"], src["id"]))
            self._ledger(conn, batch_no, order["from_warehouse"], LedgerType.TRANSFER_OUT,
                         order["qty"], order["order_no"], "排队放行发货")
            conn.execute("UPDATE transfer_orders SET status = ?, shipped_qty = ?,"
                         " updated_at = ? WHERE order_no = ?",
                         (OrderStatus.IN_TRANSIT.value, order["qty"], _now(),
                          order["order_no"]))
            self._recalc(conn, batch_no, order["from_warehouse"])

    def _recalc(self, conn, batch_no: str, warehouse_id: str) -> None:
        """重算可放量并落库：available = total - frozen - in_transit - hold。"""
        conn.execute(
            "UPDATE batches SET available_qty ="
            " MAX(total_qty - frozen_qty - in_transit_qty - hold_qty, 0)"
            " WHERE batch_no = ? AND warehouse_id = ?",
            (batch_no, warehouse_id))

    def _incoming_qty(self, conn, batch_no: str, warehouse_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS n FROM transfer_orders"
            " WHERE batch_no = ? AND to_warehouse = ? AND status = ?",
            (batch_no, warehouse_id, OrderStatus.IN_TRANSIT.value)).fetchone()
        return row["n"]

    def _queued_qty(self, conn, batch_no: str, warehouse_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS n FROM transfer_orders"
            " WHERE batch_no = ? AND to_warehouse = ? AND status = ?",
            (batch_no, warehouse_id, OrderStatus.QUEUED.value)).fetchone()
        return row["n"]

    # ------------------------------------------------------------------
    # 内部：行存取
    # ------------------------------------------------------------------

    def _find_batch(self, conn, batch_no: str, warehouse_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM batches WHERE batch_no = ? AND warehouse_id = ?",
                           (batch_no, warehouse_id)).fetchone()
        return dict(row) if row else None

    def _must_batch(self, conn, batch_no: str, warehouse_id: str) -> dict:
        row = self._find_batch(conn, batch_no, warehouse_id)
        if not row:
            raise ApiError(404, "BATCH_NOT_FOUND",
                           f"批次 {batch_no} 在仓库 {warehouse_id} 不存在")
        return row

    def _must_warehouse(self, conn, warehouse_id: str) -> dict:
        row = conn.execute("SELECT * FROM warehouses WHERE warehouse_id = ?",
                           (warehouse_id,)).fetchone()
        if not row:
            raise ApiError(404, "WAREHOUSE_NOT_FOUND", f"仓库 {warehouse_id} 未注册")
        return dict(row)

    def _find_order(self, conn, order_no: str) -> dict | None:
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?",
                           (order_no,)).fetchone()
        return dict(row) if row else None

    def _must_order(self, conn, order_no: str) -> dict:
        row = self._find_order(conn, order_no)
        if not row:
            raise ApiError(404, "ORDER_NOT_FOUND", f"调拨单 {order_no} 不存在")
        return row

    def _ensure_dest_batch(self, conn, src_batch: dict, to_warehouse: str) -> dict:
        dest = self._find_batch(conn, src_batch["batch_no"], to_warehouse)
        if dest:
            return dest
        wh = self._must_warehouse(conn, to_warehouse)
        conn.execute(
            "INSERT INTO batches(batch_no, drug_code, warehouse_id, total_qty, capacity,"
            "                   temp_min, temp_max, available_qty, created_at)"
            " VALUES (?,?,?,0,?,?,?,0,?)",
            (src_batch["batch_no"], src_batch["drug_code"], to_warehouse,
             wh["default_batch_capacity"], src_batch["temp_min"], src_batch["temp_max"],
             _now()))
        return self._must_batch(conn, src_batch["batch_no"], to_warehouse)

    def _create_order(self, conn, order_no: str, batch_no: str, from_warehouse: str,
                      to_warehouse: str, qty: int, status: OrderStatus,
                      shipped_qty: int | None = None) -> dict:
        now = _now()
        conn.execute(
            "INSERT INTO transfer_orders(order_no, batch_no, from_warehouse, to_warehouse,"
            " qty, status, shipped_qty, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (order_no, batch_no, from_warehouse, to_warehouse, qty, status.value,
             shipped_qty, now, now))
        return self._must_order(conn, order_no)

    def _ledger(self, conn, batch_no: str, warehouse_id: str, change_type: LedgerType,
                qty: int, ref_order: str | None, note: str) -> None:
        conn.execute(
            "INSERT INTO stock_ledger(batch_no, warehouse_id, change_type, qty, ref_order,"
            " note, created_at) VALUES (?,?,?,?,?,?,?)",
            (batch_no, warehouse_id, change_type.value, qty, ref_order, note, _now()))
