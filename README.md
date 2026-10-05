# 冷链药品调拨核对接口

冷库调拨药品时，同一批次的**在途量**和**可放量**常对不上。本服务提供一套调拨核对接口，覆盖：两仓并发提交的批次占用互斥、车厢断网恢复上报的幂等合并、到货短少挂跨仓对账、容量排队、温控重算可放量、超限自动冻结、旧数据期初回填。

## 运行

```bash
npm install
npm start          # 默认 :3000，可用 PORT=3001 改端口
npm test           # 运行全部 7 组场景测试
```

## 核心口径

- **可放量** `available = quantity - occupied_qty`；批次冻结时 `available = 0`。
- **在途/占用量** 记录在 `batch_occupations`，只有 `status='occupied'` 的占用会扣减可放量。
- 所有“先确认占用、再写入”的检查都在 `BEGIN IMMEDIATE` 事务内完成，并发提交时后到者必然读到前者已提交的占用。

## 接口

### 批次

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/batches` | 建档/更新批次（`batch_no, drug_name, warehouse_id, quantity, capacity, temp_min, temp_max`） |
| GET | `/api/batches` | 批次列表（含 `available`） |
| GET | `/api/temperature/logs` | 温控记录 |

### 调拨

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/allocations` | 提交调拨单 |
| GET | `/api/allocations` | 调拨单列表 |
| POST | `/api/allocations/:no/arrive` | 到货确认 |

**提交调拨单** 处理顺序：
1. 批次冻结 → 挡回（409）；
2. 批次存在未释放占用 → 挡回并返回当前单号（409，`current_allocation_no`）；
3. 申请量 > 可放量 → 挡回（409）；
4. 目的地批次容量不足 → 进入排队（`queued: true`）；
5. 否则正式占用（在途 `occupied_qty += qty`）。

**到货确认**：`arrived_qty` 不超过发出量。实际到货部分增减双方账面；**短少部分不冲减批次余量**，而是继续挂在途并生成一条 `跨仓对账待处理`。

### 车厢上报（断网恢复）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/transport/reports` | 批量上报，按 `record_no` 幂等合并 |

```json
{
  "carriage_no": "CAR-01",
  "reports": [
    { "record_no": "R1", "event_type": "dispatch", "allocation_no": "A1", "batch_no": "B1", "from_warehouse": "WH-A", "to_warehouse": "WH-B", "qty": 20 },
    { "record_no": "R2", "event_type": "arrive", "allocation_no": "A1", "arrived_qty": 20 },
    { "record_no": "R3", "event_type": "temperature", "warehouse_id": "WH-A", "batch_no": "B1", "temperature": 6 }
  ]
}
```

返回 `inserted` / `duplicates`。同一条 `record_no` 重复上报只入库一次，事件只应用一次。

### 温控 / 冻结

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/temperature` | `{ warehouse_id, batch_no, temperature }` |

温度超出 `[temp_min, temp_max]` 时：记录日志、批次置 `frozen`、`temp_exceeded=1`、可放量归零。**冻结是粘滞状态**——即使后续温度恢复正常也保持冻结，禁止调拨。

### 对账 / 排队 / 期初

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/reconciliation` | 跨仓对账待处理列表 |
| GET | `/api/queue` | 容量排队列表 |
| POST | `/api/admin/process-queue` | 手动触发排队激活（腾出容量后也会自动触发） |
| POST | `/api/admin/backfill-opening` | 期初回填 |

**期初回填**：对所有既非调出方也非调入方（即缺调拨记录）的批次，按现存数量回填 `opening_qty = quantity`；已有调拨记录的批次不回填，重复执行幂等。

## 场景示例

```bash
# 建档
curl -s -X POST localhost:3000/api/batches -H 'Content-Type: application/json' \
  -d '{"batch_no":"B001","drug_name":"胰岛素","warehouse_id":"WH-A","quantity":100,"capacity":120}'

# 提交调拨
curl -s -X POST localhost:3000/api/allocations -H 'Content-Type: application/json' \
  -d '{"allocation_no":"A001","batch_no":"B001","from_warehouse":"WH-A","to_warehouse":"WH-B","qty":30}'

# 后到的调拨单被挡回，并告知当前单号
curl -s -X POST localhost:3000/api/allocations -H 'Content-Type: application/json' \
  -d '{"allocation_no":"A002","batch_no":"B001","from_warehouse":"WH-A","to_warehouse":"WH-C","qty":10}'
# → 409 {"error":"批次占用中，当前单号: A001","current_allocation_no":"A001"}

# 到货短少：28/30，差额挂跨仓对账
curl -s -X POST localhost:3000/api/allocations/A001/arrive -H 'Content-Type: application/json' \
  -d '{"arrived_qty":28}'
# → {"diff_qty":2,"status":"reconciling"}

# 温度超限 → 自动冻结
curl -s -X POST localhost:3000/api/temperature -H 'Content-Type: application/json' \
  -d '{"warehouse_id":"WH-A","batch_no":"B001","temperature":12}'
# → {"frozen":true,"available":0}
```

## 数据模型

```
batches                批次（账面余量 / 占用量 / 容量 / 温控区间 / 冻结标志）
allocations            调拨单（occupied → reconciling/done / queued）
batch_occupations      批次占用记录（occupied → released，用于“当前单号”）
transport_reports      车厢上报记录（record_no 唯一幂等）
reconciliation_items   跨仓对账待处理（发出 - 到货 = diff_qty）
capacity_queue         容量排队（waiting → processed，FIFO）
temperature_logs       温控记录
```

## 结构

```
src/db.js        SQLite 连接与表结构（BEGIN IMMEDIATE / busy_timeout）
src/service.js   全部业务逻辑（占用、调拨、到货、排队、温控、冻结、回填）
src/server.js    Express 路由
test/service.test.js  7 组场景测试
```
