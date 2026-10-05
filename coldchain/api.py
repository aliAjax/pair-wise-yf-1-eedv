"""HTTP JSON 接口（纯标准库实现）。

错误响应统一为 {"error": {"code", "message", ...}}；
占用冲突返回 409 且 body 携带 current_order_no（当前持有占用的单号）。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .models import ApiError
from .service import ColdChainService
from .store import Store


def _need(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) is None or body.get(f) == ""]
    if missing:
        raise ApiError(422, "MISSING_FIELD", f"缺少字段: {', '.join(missing)}")


# ----------------------------------------------------------------------
# 路由处理函数：func(service, match, query, body) -> (status, payload)
# ----------------------------------------------------------------------

def _create_warehouse(svc, m, q, b):
    _need(b, "warehouse_id")
    result = svc.register_warehouse(
        b["warehouse_id"], b.get("name", ""),
        int(b.get("default_batch_capacity", 1000)))
    return 201, result


def _create_batch(svc, m, q, b):
    _need(b, "batch_no", "drug_code", "warehouse_id", "qty")
    result = svc.register_batch(
        b["batch_no"], b["drug_code"], b["warehouse_id"], int(b["qty"]),
        capacity=int(b["capacity"]) if b.get("capacity") is not None else None,
        temp_min=float(b.get("temp_min", 2.0)), temp_max=float(b.get("temp_max", 8.0)),
        record_opening=bool(b.get("record_opening", True)))
    return 201, result


def _get_batch(svc, m, q, b):
    _need(q, "warehouse_id")
    return 200, svc.get_batch(m.group(1), q["warehouse_id"])


def _batch_temperature(svc, m, q, b):
    _need(b, "warehouse_id", "temp")
    return 200, svc.record_temperature(m.group(1), b["warehouse_id"], float(b["temp"]))


def _batch_temp_range(svc, m, q, b):
    _need(b, "warehouse_id", "temp_min", "temp_max")
    return 200, svc.update_temp_range(m.group(1), b["warehouse_id"],
                                      float(b["temp_min"]), float(b["temp_max"]))


def _batch_unfreeze(svc, m, q, b):
    _need(b, "warehouse_id")
    return 200, svc.unfreeze_batch(m.group(1), b["warehouse_id"], b.get("note", ""))


def _batch_capacity(svc, m, q, b):
    _need(b, "warehouse_id", "capacity")
    return 200, svc.update_capacity(m.group(1), b["warehouse_id"], int(b["capacity"]))


def _create_transfer(svc, m, q, b):
    _need(b, "order_no", "batch_no", "from_warehouse", "to_warehouse", "qty")
    order, created = svc.submit_transfer(b["order_no"], b["batch_no"],
                                         b["from_warehouse"], b["to_warehouse"],
                                         int(b["qty"]))
    if not created:
        return 200, order            # 幂等重提
    if order["status"] == "QUEUED":
        return 202, order            # 容量满，已排队
    return 201, order


def _get_transfer(svc, m, q, b):
    return 200, svc.get_order(m.group(1))


def _cancel_transfer(svc, m, q, b):
    return 200, svc.cancel_transfer(m.group(1))


def _merge_reports(svc, m, q, b):
    _need(b, "reports")
    if not isinstance(b["reports"], list):
        raise ApiError(422, "INVALID_FIELD", "reports 必须是数组")
    return 200, svc.merge_transport_reports(b["reports"])


def _list_reconciliation(svc, m, q, b):
    return 200, {"items": svc.list_reconciliation(q.get("status"))}


def _resolve_reconciliation(svc, m, q, b):
    _need(b, "resolution")
    return 200, svc.resolve_reconciliation(int(m.group(1)), b["resolution"])


def _backfill(svc, m, q, b):
    return 200, svc.backfill_opening()


def _list_queue(svc, m, q, b):
    return 200, {"items": svc.list_queue(q.get("batch_no"), q.get("warehouse_id"))}


def _list_ledger(svc, m, q, b):
    return 200, {"items": svc.list_ledger(q.get("batch_no"), q.get("warehouse_id"))}


ROUTES = [
    ("POST", re.compile(r"/api/warehouses"), _create_warehouse),
    ("POST", re.compile(r"/api/batches"), _create_batch),
    ("GET", re.compile(r"/api/batches/([^/]+)"), _get_batch),
    ("POST", re.compile(r"/api/batches/([^/]+)/temperature"), _batch_temperature),
    ("POST", re.compile(r"/api/batches/([^/]+)/temp-range"), _batch_temp_range),
    ("POST", re.compile(r"/api/batches/([^/]+)/unfreeze"), _batch_unfreeze),
    ("POST", re.compile(r"/api/batches/([^/]+)/capacity"), _batch_capacity),
    ("POST", re.compile(r"/api/transfers"), _create_transfer),
    ("GET", re.compile(r"/api/transfers/([^/]+)"), _get_transfer),
    ("POST", re.compile(r"/api/transfers/([^/]+)/cancel"), _cancel_transfer),
    ("POST", re.compile(r"/api/transport/reports:merge"), _merge_reports),
    ("GET", re.compile(r"/api/reconciliation"), _list_reconciliation),
    ("POST", re.compile(r"/api/reconciliation/(\d+)/resolve"), _resolve_reconciliation),
    ("POST", re.compile(r"/api/backfill-opening"), _backfill),
    ("GET", re.compile(r"/api/queue"), _list_queue),
    ("GET", re.compile(r"/api/ledger"), _list_ledger),
]


def make_handler(service: ColdChainService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静默访问日志
            pass

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                body = self._read_body() if method == "POST" else {}
                for m, pattern, func in ROUTES:
                    if m != method:
                        continue
                    match = pattern.fullmatch(parsed.path)
                    if match:
                        status, payload = func(service, match, query, body)
                        self._send(status, payload)
                        return
                raise ApiError(404, "NOT_FOUND",
                               f"接口不存在: {method} {parsed.path}")
            except ApiError as e:
                self._send(e.status, e.to_dict())
            except Exception as e:  # noqa: BLE001 - 兜底，避免连接悬挂
                self._send(500, {"error": {"code": "INTERNAL", "message": str(e)}})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(db_path: str = "coldchain.db", host: str = "127.0.0.1", port: int = 8080):
    service = ColdChainService(Store(db_path))
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"冷库调拨核对服务已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
