"""调拨核对核心业务逻辑测试。"""
import unittest

from coldchain import ApiError, BatchOccupiedError, ColdChainService, Store


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.svc = ColdChainService(Store())
        self.svc.register_warehouse("WH-A", "华东冷库", default_batch_capacity=1000)
        self.svc.register_warehouse("WH-B", "华南冷库", default_batch_capacity=1000)
        self.svc.register_batch("BN-001", "DRUG-01", "WH-A", 500,
                                temp_min=2.0, temp_max=8.0)

    def batch(self, batch_no="BN-001", wh="WH-A"):
        return self.svc.get_batch(batch_no, wh)

    def arrive(self, order_no, received_qty, report_id="RPT-1"):
        return self.svc.merge_transport_reports([{
            "report_id": report_id,
            "order_no": order_no,
            "events": [{"type": "ARRIVED", "received_qty": received_qty}],
        }])


class TestOccupancy(ServiceTestBase):
    def test_later_submission_blocked_with_current_order_no(self):
        """后到的那次先确认批次占用：未释放就挡回去并告知当前单号。"""
        order, created = self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.assertTrue(created)
        self.assertEqual(order["status"], "IN_TRANSIT")

        with self.assertRaises(BatchOccupiedError) as ctx:
            self.svc.submit_transfer("T-002", "BN-001", "WH-A", "WH-B", 50)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.extra["current_order_no"], "T-001")

    def test_occupancy_released_after_arrival(self):
        """占用释放（到货）后，被挡回的单可以重新提交。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        with self.assertRaises(BatchOccupiedError):
            self.svc.submit_transfer("T-002", "BN-001", "WH-A", "WH-B", 50)
        self.arrive("T-001", 100)
        order, created = self.svc.submit_transfer("T-002", "BN-001", "WH-A", "WH-B", 50)
        self.assertTrue(created)
        self.assertEqual(order["status"], "IN_TRANSIT")

    def test_same_batch_no_in_different_warehouse_not_blocked(self):
        """占用键是(批次号, 发出仓)：不同仓的同批次号物理库存独立。"""
        self.svc.register_batch("BN-001", "DRUG-01", "WH-B", 200)
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        order, created = self.svc.submit_transfer("T-002", "BN-001", "WH-B", "WH-A", 30)
        self.assertTrue(created)
        self.assertEqual(order["status"], "IN_TRANSIT")

    def test_idempotent_resubmit(self):
        """同一单号重复提交返回原单，不重复扣减。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        order, created = self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.assertFalse(created)
        self.assertEqual(self.batch()["in_transit_qty"], 100)
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 999)
        self.assertEqual(ctx.exception.code, "ORDER_NO_CONFLICT")

    def test_insufficient_available_releases_occupancy(self):
        """可放不足被拒后占用不残留，可立即用合法数量重提。"""
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 600)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_AVAILABLE")
        order, _ = self.svc.submit_transfer("T-002", "BN-001", "WH-A", "WH-B", 100)
        self.assertEqual(order["status"], "IN_TRANSIT")


class TestTransportMerge(ServiceTestBase):
    def test_duplicate_report_stored_once(self):
        """断网恢复后合并：同一条记录重复上报只入库一次。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        report = {"report_id": "RPT-9", "order_no": "T-001",
                  "events": [{"type": "DEPARTED"},
                             {"type": "ARRIVED", "received_qty": 100}]}
        # 车厢断网恢复，同一批记录补报两次
        r1 = self.svc.merge_transport_reports([report, dict(report)])
        self.assertEqual([r["result"] for r in r1["results"]], ["MERGED", "DUPLICATE"])
        r2 = self.svc.merge_transport_reports([report])
        self.assertEqual(r2["results"][0]["result"], "DUPLICATE")
        # 到货只入账一次
        self.assertEqual(self.batch("BN-001", "WH-B")["total_qty"], 100)
        self.assertEqual(self.batch()["total_qty"], 400)

    def test_duplicate_arrival_event_not_double_counted(self):
        """不同 report_id 重复携带同一单的到货事件，也只入账一次。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.arrive("T-001", 100, report_id="RPT-1")
        self.arrive("T-001", 100, report_id="RPT-2")
        self.assertEqual(self.batch("BN-001", "WH-B")["total_qty"], 100)

    def test_shortage_creates_reconciliation_and_keeps_balance(self):
        """到货少于发出：差额挂跨仓对账待处理，批次余量不跟着改。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.arrive("T-001", 95)

        items = self.svc.list_reconciliation(status="PENDING")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 5)
        self.assertEqual(items[0]["reason"], "SHORTAGE")
        self.assertEqual(items[0]["from_warehouse"], "WH-A")
        self.assertEqual(items[0]["to_warehouse"], "WH-B")

        src = self.batch()
        # 批次余量只减实收 95，短缺 5 仍挂在账上（余量不跟着改）
        self.assertEqual(src["total_qty"], 405)
        self.assertEqual(src["in_transit_qty"], 0)
        # 但挂账部分不可放
        self.assertEqual(src["hold_qty"], 5)
        self.assertEqual(src["available_qty"], 400)

    def test_reconciliation_write_off(self):
        """对账确认损耗：此时才核减批次账面。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.arrive("T-001", 95)
        item_id = self.svc.list_reconciliation("PENDING")[0]["id"]
        self.svc.resolve_reconciliation(item_id, "WRITE_OFF")
        src = self.batch()
        self.assertEqual(src["total_qty"], 400)
        self.assertEqual(src["hold_qty"], 0)
        self.assertEqual(src["available_qty"], 400)
        self.assertEqual(self.svc.list_reconciliation("PENDING"), [])

    def test_reconciliation_release_to_source(self):
        """货物找回：解除挂账，余量恢复可放。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.arrive("T-001", 95)
        item_id = self.svc.list_reconciliation("PENDING")[0]["id"]
        self.svc.resolve_reconciliation(item_id, "RELEASE_TO_SOURCE")
        src = self.batch()
        self.assertEqual(src["total_qty"], 405)
        self.assertEqual(src["hold_qty"], 0)
        self.assertEqual(src["available_qty"], 405)

    def test_reconciliation_deliver_to_dest(self):
        """补记到货：挂账量转入目的仓。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.arrive("T-001", 95)
        item_id = self.svc.list_reconciliation("PENDING")[0]["id"]
        self.svc.resolve_reconciliation(item_id, "DELIVER_TO_DEST")
        self.assertEqual(self.batch()["total_qty"], 400)
        self.assertEqual(self.batch("BN-001", "WH-B")["total_qty"], 100)

    def test_reconciliation_resolve_twice_rejected(self):
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.arrive("T-001", 95)
        item_id = self.svc.list_reconciliation("PENDING")[0]["id"]
        self.svc.resolve_reconciliation(item_id, "WRITE_OFF")
        with self.assertRaises(ApiError) as ctx:
            self.svc.resolve_reconciliation(item_id, "WRITE_OFF")
        self.assertEqual(ctx.exception.code, "RECON_ALREADY_RESOLVED")


class TestCapacityQueue(ServiceTestBase):
    def test_full_capacity_queues_and_drains_in_order(self):
        """批次容量满了就排队；容量腾出后按提交顺序放行。"""
        self.svc.register_batch("BN-001", "DRUG-01", "WH-B", 80, capacity=100)
        o1, _ = self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 30)
        self.assertEqual(o1["status"], "QUEUED")  # 80+30 > 100
        # 排队单持有占用，同批次后续调拨被挡
        with self.assertRaises(BatchOccupiedError) as ctx:
            self.svc.submit_transfer("T-002", "BN-001", "WH-A", "WH-B", 10)
        self.assertEqual(ctx.exception.extra["current_order_no"], "T-001")

        # 扩容 → 队头放行
        self.svc.update_capacity("BN-001", "WH-B", 200)
        self.assertEqual(self.svc.get_order("T-001")["status"], "IN_TRANSIT")
        self.assertEqual(self.batch()["in_transit_qty"], 30)

    def test_fifo_head_of_line_blocking(self):
        """严格 FIFO：队头放不下时，后面的小单也不插队。"""
        self.svc.register_warehouse("WH-C", "华西冷库")
        self.svc.register_batch("BN-001", "DRUG-01", "WH-C", 300)
        self.svc.register_batch("BN-001", "DRUG-01", "WH-B", 80, capacity=100)
        # 两个不同来源仓的排队单进入同一 (批次, 目的仓) 队列
        o1, _ = self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 30)
        o2, _ = self.svc.submit_transfer("T-002", "BN-001", "WH-C", "WH-B", 10)
        self.assertEqual(o1["status"], "QUEUED")  # 80+30 > 100
        self.assertEqual(o2["status"], "QUEUED")  # 80+30(排队预留不算)+10 > 100

        # 容量到 95：余量 15 放不下队头 T-001(30)，T-002(10) 也不能插队
        self.svc.update_capacity("BN-001", "WH-B", 95)
        self.assertEqual(self.svc.get_order("T-001")["status"], "QUEUED")
        self.assertEqual(self.svc.get_order("T-002")["status"], "QUEUED")

        # 容量到 110：队头放行后无余量，T-002 继续等
        self.svc.update_capacity("BN-001", "WH-B", 110)
        self.assertEqual(self.svc.get_order("T-001")["status"], "IN_TRANSIT")
        self.assertEqual(self.svc.get_order("T-002")["status"], "QUEUED")

        # 容量到 120：T-002 放行
        self.svc.update_capacity("BN-001", "WH-B", 120)
        self.assertEqual(self.svc.get_order("T-002")["status"], "IN_TRANSIT")

    def test_cancel_queued_releases_occupancy(self):
        self.svc.register_batch("BN-001", "DRUG-01", "WH-B", 100, capacity=100)
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 10)  # QUEUED
        self.svc.cancel_transfer("T-001")
        self.assertEqual(self.svc.get_order("T-001")["status"], "CANCELLED")
        order, _ = self.svc.submit_transfer("T-002", "BN-001", "WH-A", "WH-B", 10)
        self.assertEqual(order["status"], "QUEUED")  # 容量仍满，但占用已释放可重新排队


class TestTemperature(ServiceTestBase):
    def test_excursion_auto_freeze(self):
        """温度超限过的库存自动冻结，可放量归零。"""
        self.svc.record_temperature("BN-001", "WH-A", 9.5)
        b = self.batch()
        self.assertTrue(b["temp_violated"])
        self.assertEqual(b["frozen_qty"], 500)
        self.assertEqual(b["available_qty"], 0)
        # 冻结后不能调拨
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 10)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_AVAILABLE")

    def test_freeze_not_auto_released_when_temp_recovers(self):
        """温度回落不自动解冻，需 QA 人工放行。"""
        self.svc.record_temperature("BN-001", "WH-A", 9.5)
        self.svc.record_temperature("BN-001", "WH-A", 5.0)
        self.assertEqual(self.batch()["frozen_qty"], 500)
        self.svc.unfreeze_batch("BN-001", "WH-A", "QA 复核放行")
        b = self.batch()
        self.assertEqual(b["frozen_qty"], 0)
        self.assertEqual(b["available_qty"], 500)
        self.assertTrue(b["temp_violated"])  # 超限事实保留

    def test_temperature_change_recalculates_available(self):
        """温控变化就重算可放量。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.assertEqual(self.batch()["available_qty"], 400)
        # 一次普通温度上报（未超限）也触发重算
        b = self.svc.record_temperature("BN-001", "WH-A", 4.0)
        self.assertEqual(b["available_qty"], 400)
        self.assertEqual(b["frozen_qty"], 0)

    def test_temp_range_tightening_freezes_stock(self):
        """温控标准收紧后，按最近温度重新评估并冻结。"""
        self.svc.record_temperature("BN-001", "WH-A", 6.0)
        self.assertEqual(self.batch()["frozen_qty"], 0)
        self.svc.update_temp_range("BN-001", "WH-A", 2.0, 5.0)
        b = self.batch()
        self.assertEqual(b["frozen_qty"], 500)
        self.assertEqual(b["available_qty"], 0)

    def test_truck_excursion_freezes_on_arrival(self):
        """在途温度超限：到货自动冻结入目的仓。"""
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 100)
        self.svc.merge_transport_reports([{
            "report_id": "RPT-T", "order_no": "T-001",
            "events": [{"type": "TEMP_READING", "temp": 12.0},
                       {"type": "ARRIVED", "received_qty": 100}],
        }])
        dst = self.batch("BN-001", "WH-B")
        self.assertTrue(dst["temp_violated"])
        self.assertEqual(dst["total_qty"], 100)
        self.assertEqual(dst["frozen_qty"], 100)
        self.assertEqual(dst["available_qty"], 0)
        self.assertTrue(self.svc.get_order("T-001")["temp_excursion"])

    def test_freeze_cancels_queued_orders(self):
        """批次冻结后，排队中的调出单取消并释放占用。"""
        self.svc.register_batch("BN-001", "DRUG-01", "WH-B", 100, capacity=100)
        self.svc.submit_transfer("T-001", "BN-001", "WH-A", "WH-B", 10)  # QUEUED
        self.svc.record_temperature("BN-001", "WH-A", 10.0)
        self.assertEqual(self.svc.get_order("T-001")["status"], "CANCELLED")


class TestBackfill(ServiceTestBase):
    def test_backfill_opening_for_legacy_batches(self):
        """旧数据缺调拨记录的批次按现存数量回填期初，且幂等。"""
        # 模拟旧系统迁移来的批次：有现存数量，没有任何流水
        self.svc.register_batch("BN-OLD", "DRUG-09", "WH-A", 320, record_opening=False)
        result = self.svc.backfill_opening()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["backfilled"][0],
                         {"batch_no": "BN-OLD", "warehouse_id": "WH-A", "qty": 320})
        ledger = self.svc.list_ledger("BN-OLD", "WH-A")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["change_type"], "OPENING_BACKFILL")
        self.assertEqual(ledger[0]["qty"], 320)
        # 再跑一次不重复回填
        self.assertEqual(self.svc.backfill_opening()["count"], 0)

    def test_normal_batches_not_backfilled(self):
        """已有流水的正常批次不参与回填。"""
        result = self.svc.backfill_opening()
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main()
