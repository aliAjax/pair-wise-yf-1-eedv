# 冷库药品调拨核对服务

针对"同一批次在途量和可放量对不上"的问题，提供一套调拨核对接口。纯 Python 标准库实现（SQLite 持久化），零依赖。

## 运行与测试

```bash
python3 run_server.py            # 默认 127.0.0.1:8080，数据落 coldchain.db
python3 run_server.py 0.0.0.0 8080 /data/coldchain.db
python3 -m unittest discover     # 26 个测试
```

## 核心语义

**可放量公式**：`available = 账面余量 total - 冻结 frozen - 在途 in_transit - 对账挂账 hold`，每次相关变动后重算落库，任何时刻可放、在途、账面三者对得上。

**批次占用**：占用键 = `(批次号, 发出仓)`，由数据库部分唯一索引兜底。调拨单提交时先确认占用：
- 占用未释放 → `409 BATCH_OCCUPIED`，响应携带 `current_order_no`（当前持有占用的单号）；
- 占用随 `QUEUED` / `IN_TRANSIT` 单持有，到货或取消时释放；
- 不同仓的同批次号物理库存独立，互不阻塞；校验失败（如可放不足）事务回滚，占用不残留。

**断网合并**：车厢离线期间缓存的上报恢复后批量提交，按 `report_id` 去重——同一条记录重复上报只入库一次（`DUPLICATE`）；订单级幂等兜底，不同记录重复携带同一到货事件也不重复入账。每条记录独立事务，单条出错不影响其余合并。

**到货短缺**：实收 < 发出时，差额挂 `reconciliation_items`（跨仓对账待处理）；源仓账面只减实收量，短缺部分进 `hold_qty`（批次余量不跟着改，但挂账量不可放）。核销时才动账面：
- `WRITE_OFF` 确认损耗：核减源仓账面与挂账；
- `RELEASE_TO_SOURCE` 货物找回：解除挂账，恢复可放；
- `DELIVER_TO_DEST` 补记到货：挂账量转入目的仓。

**容量排队**：目的仓容量检查把在途与排队中的量都算作已预留（后到小单不会插队）。容量满 → `202 QUEUED`；扩容、到货、核销腾出容量后按提交顺序严格 FIFO 放行（队头放不下就停）。

**温控**：任何温度上报/温控标准变化都触发可放量重算。温度超限 → 该仓该批次在库库存自动冻结（单向，温度回落不自动解冻，需 QA 人工放行）；在途超限标记 `temp_excursion`，到货直接进目的仓冻结量；批次冻结时其排队中的调出单自动取消并释放占用。

**期初回填**：旧系统迁移来、没有任何流水的批次（`record_opening=false` 导入），按现存数量回填 `OPENING_BACKFILL` 期初流水，幂等。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/warehouses` | 注册仓库 `{warehouse_id, name?, default_batch_capacity?}` |
| POST | `/api/batches` | 登记批次 `{batch_no, drug_code, warehouse_id, qty, capacity?, temp_min?, temp_max?, record_opening?}` |
| GET | `/api/batches/{batch_no}?warehouse_id=` | 批次视图（含重算后的可放量） |
| POST | `/api/batches/{batch_no}/temperature` | 温度上报 `{warehouse_id, temp}`：重算可放量，超限自动冻结 |
| POST | `/api/batches/{batch_no}/temp-range` | 调整温控标准 `{warehouse_id, temp_min, temp_max}`，按最近温度重估 |
| POST | `/api/batches/{batch_no}/unfreeze` | QA 人工解冻 `{warehouse_id, note?}` |
| POST | `/api/batches/{batch_no}/capacity` | 调整容量 `{warehouse_id, capacity}`，触发排队放行 |
| POST | `/api/transfers` | 提交调拨单 `{order_no, batch_no, from_warehouse, to_warehouse, qty}` → `201` 在途 / `202` 排队 / `409` 占用冲突（含 `current_order_no`）/ `200` 幂等重提 |
| GET | `/api/transfers/{order_no}` | 查询调拨单 |
| POST | `/api/transfers/{order_no}/cancel` | 取消排队单并释放占用（在途单不可取消） |
| POST | `/api/transport/reports:merge` | 合并车厢上报 `{reports: [{report_id, order_no, events: [...]}]}`，事件：`DEPARTED` / `TEMP_READING{temp}` / `ARRIVED{received_qty}` |
| GET | `/api/reconciliation?status=PENDING` | 跨仓对账待处理列表 |
| POST | `/api/reconciliation/{id}/resolve` | 核销 `{resolution: WRITE_OFF\|RELEASE_TO_SOURCE\|DELIVER_TO_DEST}` |
| POST | `/api/backfill-opening` | 旧数据按现存数量回填期初（幂等） |
| GET | `/api/queue?batch_no=&warehouse_id=` | 排队中的调拨单 |
| GET | `/api/ledger?batch_no=&warehouse_id=` | 库存流水 |

错误响应统一为 `{"error": {"code", "message", ...}}`。

## 典型流程

```bash
# 1. 双仓并发提交同一批次 → 后到者拿到 409 与当前单号
curl -X POST :8080/api/transfers -d '{"order_no":"DB001","batch_no":"P001","from_warehouse":"SH","to_warehouse":"GZ","qty":200}'
curl -X POST :8080/api/transfers -d '{"order_no":"DB002","batch_no":"P001","from_warehouse":"SH","to_warehouse":"GZ","qty":100}'
# → {"error":{"code":"BATCH_OCCUPIED","current_order_no":"DB001",...}}

# 2. 车厢断网恢复，合并上报（重复记录自动去重；在途超温 → 到货自动冻结；短缺挂对账）
curl -X POST :8080/api/transport/reports:merge -d '{"reports":[
  {"report_id":"GPS-881","order_no":"DB001","events":[
    {"type":"TEMP_READING","temp":11.2},
    {"type":"ARRIVED","received_qty":190}]}]}'

# 3. 占用已释放，DB002 可重提；短缺 10 挂 PENDING 对账，批次余量未动
curl -X POST :8080/api/transfers -d '{"order_no":"DB002",...}'
curl :8080/api/reconciliation?status=PENDING

# 4. 对账核销（此时才动账面）
curl -X POST :8080/api/reconciliation/1/resolve -d '{"resolution":"WRITE_OFF"}'
```

## 设计说明

- **占用键为何带发出仓**：库存是物理的，同批次号在不同仓是独立库存。若业务要求同批次号全局互斥，把 `_acquire_occupancy` 的键去掉 `warehouse_id` 即可（一处改动）。
- **短缺为何挂 `hold_qty` 而不是直接改余量**：账面余量保持不动满足"余量不跟着改"，同时 `hold` 把争议量排除出可放，防止短缺部分被再次调出，两边账始终对得上。
- **冻结为何单向**："超限过"是质量事实（`temp_violated` 永久留痕），解冻属于 QA 质量放行决策，不随温度回落自动发生。
