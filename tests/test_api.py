"""HTTP 接口层测试：端到端流程 + 双仓并发提交互斥。"""
import http.client
import json
import threading
import unittest

from coldchain import ColdChainService, Store
from coldchain.api import make_handler

from http.server import ThreadingHTTPServer


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = ColdChainService(Store())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def get(self, path):
        return self.request("GET", path)


class TestEndToEnd(ApiTestBase):
    def test_full_transfer_flow(self):
        """注册 → 调拨 → 断网恢复合并上报 → 短缺对账 → 核销，全链路走通。"""
        self.post("/api/warehouses", {"warehouse_id": "E2E-A", "name": "甲仓"})
        self.post("/api/warehouses", {"warehouse_id": "E2E-B", "name": "乙仓"})
        status, _ = self.post("/api/batches", {
            "batch_no": "E2E-BN", "drug_code": "D-1", "warehouse_id": "E2E-A",
            "qty": 200, "temp_min": 2, "temp_max": 8})
        self.assertEqual(status, 201)

        status, order = self.post("/api/transfers", {
            "order_no": "E2E-T1", "batch_no": "E2E-BN",
            "from_warehouse": "E2E-A", "to_warehouse": "E2E-B", "qty": 120})
        self.assertEqual(status, 201)
        self.assertEqual(order["status"], "IN_TRANSIT")

        # 车厢断网期间缓存的记录恢复后一次性合并（含一条重复记录）
        report = {"report_id": "E2E-R1", "order_no": "E2E-T1",
                  "events": [{"type": "DEPARTED"},
                             {"type": "TEMP_READING", "temp": 5.5},
                             {"type": "ARRIVED", "received_qty": 110}]}
        status, merged = self.post("/api/transport/reports:merge",
                                   {"reports": [report, dict(report)]})
        self.assertEqual(status, 200)
        self.assertEqual([r["result"] for r in merged["results"]],
                         ["MERGED", "DUPLICATE"])

        # 短缺 10 → 跨仓对账待处理，批次余量不跟着改
        status, recon = self.get("/api/reconciliation?status=PENDING")
        self.assertEqual(len(recon["items"]), 1)
        self.assertEqual(recon["items"][0]["qty"], 10)
        status, src = self.get("/api/batches/E2E-BN?warehouse_id=E2E-A")
        self.assertEqual(src["total_qty"], 90)   # 只减实收 110
        self.assertEqual(src["hold_qty"], 10)
        self.assertEqual(src["available_qty"], 80)

        # 核销损耗后才动账面
        item_id = recon["items"][0]["id"]
        status, _ = self.post(f"/api/reconciliation/{item_id}/resolve",
                              {"resolution": "WRITE_OFF"})
        self.assertEqual(status, 200)
        _, src = self.get("/api/batches/E2E-BN?warehouse_id=E2E-A")
        self.assertEqual(src["total_qty"], 80)
        self.assertEqual(src["available_qty"], 80)

    def test_unknown_route_404(self):
        status, body = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")


class TestConcurrentSubmit(ApiTestBase):
    def test_two_warehouses_submit_simultaneously(self):
        """两个仓库同时提交调拨单：后到者被挡回并拿到当前单号。"""
        self.post("/api/warehouses", {"warehouse_id": "CC-A"})
        self.post("/api/warehouses", {"warehouse_id": "CC-B"})
        self.post("/api/batches", {"batch_no": "CC-BN", "drug_code": "D-9",
                                   "warehouse_id": "CC-A", "qty": 300})

        barrier = threading.Barrier(2)
        results = {}

        def fire(order_no):
            barrier.wait()
            results[order_no] = self.post("/api/transfers", {
                "order_no": order_no, "batch_no": "CC-BN",
                "from_warehouse": "CC-A", "to_warehouse": "CC-B", "qty": 100})

        threads = [threading.Thread(target=fire, args=(f"CC-T{i}",)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = sorted(v[0] for v in results.values())
        self.assertEqual(statuses, [201, 409], f"并发结果异常: {results}")

        winner = next(k for k, v in results.items() if v[0] == 201)
        loser_body = next(v[1] for v in results.values() if v[0] == 409)
        self.assertEqual(loser_body["error"]["code"], "BATCH_OCCUPIED")
        # 挡回时告知当前持有占用的单号
        self.assertEqual(loser_body["error"]["current_order_no"], winner)

        # 在途量只记了一次，可放量与在途量对得上
        _, batch = self.get("/api/batches/CC-BN?warehouse_id=CC-A")
        self.assertEqual(batch["in_transit_qty"], 100)
        self.assertEqual(batch["available_qty"], 200)


if __name__ == "__main__":
    unittest.main()
