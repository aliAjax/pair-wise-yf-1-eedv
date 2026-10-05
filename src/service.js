const { getDb, initDb } = require('./db');

const round4 = (v) => Math.round(v * 10000) / 10000;

class BizError extends Error {
  constructor(message, status = 400, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

/** 事务包装：BEGIN IMMEDIATE 串行化“先确认占用”的检查与写入 */
function tx(fn) {
  const d = getDb();
  d.exec('BEGIN IMMEDIATE');
  try {
    const r = fn();
    d.exec('COMMIT');
    return r;
  } catch (e) {
    try { d.exec('ROLLBACK'); } catch (_) { /* ignore */ }
    throw e;
  }
}

function getBatch(warehouseId, batchNo) {
  return getDb()
    .prepare('SELECT * FROM batches WHERE warehouse_id=? AND batch_no=?')
    .get(warehouseId, batchNo);
}

function getBatchById(id) {
  return getDb().prepare('SELECT * FROM batches WHERE id=?').get(id);
}

function getAllocationByNo(allocationNo) {
  return getDb().prepare('SELECT * FROM allocations WHERE allocation_no=?').get(allocationNo);
}

/** 可放量 = 账面余量 - 占用/在途量；冻结库存可放量为 0 */
function availableQty(batch) {
  if (batch.status === 'frozen') return 0;
  return round4(batch.quantity - batch.occupied_qty);
}

function withAvailable(b) {
  return { ...b, available: availableQty(b) };
}

/* ----------------------------- 批次建档 / 查询 ----------------------------- */

function createBatch(input) {
  const {
    batch_no, drug_name, warehouse_id,
    quantity = 0, capacity = 1000, temp_min = 2, temp_max = 8,
  } = input;
  if (!batch_no || !drug_name || !warehouse_id) {
    throw new BizError('batch_no / drug_name / warehouse_id 必填', 400);
  }
  return tx(() => {
    const d = getDb();
    const exist = getBatch(warehouse_id, batch_no);
    if (exist) {
      d.prepare("UPDATE batches SET drug_name=?, quantity=?, capacity=?, temp_min=?, temp_max=?, updated_at=CURRENT_TIMESTAMP WHERE id=?")
        .run(drug_name, quantity, capacity, temp_min, temp_max, exist.id);
      return withAvailable(getBatch(warehouse_id, batch_no));
    }
    d.prepare(`INSERT INTO batches (batch_no,drug_name,warehouse_id,quantity,occupied_qty,capacity,temp_min,temp_max,status)
               VALUES (?,?,?,? ,0, ?,?,?, 'normal')`)
      .run(batch_no, drug_name, warehouse_id, quantity, capacity, temp_min, temp_max);
    return withAvailable(getBatch(warehouse_id, batch_no));
  });
}

function listBatches() {
  return getDb().prepare('SELECT * FROM batches ORDER BY id').all().map(withAvailable);
}

function listAllocations() {
  return getDb().prepare('SELECT * FROM allocations ORDER BY id').all();
}

function listReconciliation() {
  return getDb().prepare('SELECT * FROM reconciliation_items ORDER BY id').all();
}

function listQueue() {
  return getDb().prepare('SELECT * FROM capacity_queue ORDER BY id').all();
}

function listTemperatureLogs() {
  return getDb().prepare('SELECT * FROM temperature_logs ORDER BY id').all();
}

/* ----------------------------- 提交调拨单 ----------------------------- */

function submitAllocation(input) {
  const { allocation_no, batch_no, from_warehouse, to_warehouse } = input;
  const qty = Number(input.qty);
  if (!allocation_no || !batch_no || !from_warehouse || !to_warehouse || !(qty > 0)) {
    throw new BizError('参数不完整或调拨数量非法', 400);
  }
  if (from_warehouse === to_warehouse) throw new BizError('调出仓与调入仓不能相同', 400);

  return tx(() => {
    const d = getDb();
    const src = getBatch(from_warehouse, batch_no);
    if (!src) throw new BizError(`批次不存在: ${from_warehouse}/${batch_no}`, 404);
    if (src.status === 'frozen') {
      throw new BizError('批次已冻结，禁止调拨', 409, { batch_no, status: src.status });
    }

    // 后到的这次先确认批次占用：存在未释放占用则挡回，并告知当前单号
    const occ = d.prepare("SELECT allocation_no FROM batch_occupations WHERE batch_id=? AND status='occupied' ORDER BY id LIMIT 1")
      .get(src.id);
    if (occ) {
      throw new BizError(`批次占用中，当前单号: ${occ.allocation_no}`, 409, {
        current_allocation_no: occ.allocation_no,
      });
    }

    const avail = availableQty(src);
    if (qty > avail) {
      throw new BizError(`可放量不足: 当前可放量 ${avail}, 申请 ${qty}`, 409, { available: avail });
    }

    // 目的地批次（缺省自动建档，容量随源批次）
    let dest = getBatch(to_warehouse, batch_no);
    if (!dest) {
      d.prepare(`INSERT INTO batches (batch_no,drug_name,warehouse_id,quantity,occupied_qty,capacity,temp_min,temp_max,status)
                 VALUES (?,?,?,0,0,?,?,?, 'normal')`)
        .run(batch_no, src.drug_name, to_warehouse, src.capacity, src.temp_min, src.temp_max);
      dest = getBatch(to_warehouse, batch_no);
    }

    // 批次容量满了 → 排队
    if (dest.quantity + qty > dest.capacity) {
      d.prepare(`INSERT INTO allocations (allocation_no,batch_id,destination_batch_id,from_warehouse,to_warehouse,dispatch_qty,arrived_qty,status)
                 VALUES (?,?,?,?,?,?,0,'queued')`)
        .run(allocation_no, src.id, dest.id, from_warehouse, to_warehouse, qty);
      d.prepare(`INSERT INTO capacity_queue (batch_id,allocation_no,qty,status) VALUES (?,?,?,'waiting')`)
        .run(dest.id, allocation_no, qty);
      const pos = d.prepare("SELECT COUNT(*) c FROM capacity_queue WHERE batch_id=? AND status='waiting'").get(dest.id).c;
      return { queued: true, allocation_no, queue_position: pos };
    }

    createOccupiedAllocation(src, dest, allocation_no, qty);
    return {
      queued: false,
      allocation_no,
      available_after: availableQty(getBatch(from_warehouse, batch_no)),
    };
  });
}

function createOccupiedAllocation(src, dest, allocationNo, qty) {
  const d = getDb();
  d.prepare(`INSERT INTO allocations (allocation_no,batch_id,destination_batch_id,from_warehouse,to_warehouse,dispatch_qty,arrived_qty,status)
             VALUES (?,?,?,?,?,?,0,'occupied')`)
    .run(allocationNo, src.id, dest.id, src.warehouse_id, dest.warehouse_id, qty);
  d.prepare(`INSERT INTO batch_occupations (batch_id,allocation_no,qty,status) VALUES (?,?,?,'occupied')`)
    .run(src.id, allocationNo, qty);
  d.prepare('UPDATE batches SET occupied_qty = occupied_qty + ?, updated_at=CURRENT_TIMESTAMP WHERE id=?')
    .run(qty, src.id);
}

/* ----------------------------- 到货确认 ----------------------------- */

function arriveAllocation(allocationNo, arrivedQtyInput) {
  const arrivedQty = Number(arrivedQtyInput);
  if (!(arrivedQty >= 0)) throw new BizError('到货数量非法', 400);

  return tx(() => {
    const d = getDb();
    const alloc = getAllocationByNo(allocationNo);
    if (!alloc) throw new BizError(`调拨单不存在: ${allocationNo}`, 404);
    if (alloc.status !== 'occupied') {
      throw new BizError(`调拨单状态(${alloc.status})不允许到货确认`, 409, { status: alloc.status });
    }

    const arrived = Math.min(arrivedQty, alloc.dispatch_qty);
    const diff = round4(alloc.dispatch_qty - arrived);
    const src = getBatchById(alloc.batch_id);
    const dest = getBatchById(alloc.destination_batch_id);

    // 先整笔释放占用
    d.prepare("UPDATE batch_occupations SET status='released', released_at=CURRENT_TIMESTAMP WHERE batch_id=? AND allocation_no=? AND status='occupied'")
      .run(src.id, allocationNo);
    d.prepare('UPDATE batches SET occupied_qty = occupied_qty - ? WHERE id=?')
      .run(alloc.dispatch_qty, src.id);

    if (diff > 0) {
      // 差额继续挂在途（跨仓对账待处理），不冲减批次余量
      d.prepare(`INSERT INTO batch_occupations (batch_id,allocation_no,qty,status) VALUES (?,?,?,'occupied')`)
        .run(src.id, allocationNo, diff);
      d.prepare('UPDATE batches SET occupied_qty = occupied_qty + ? WHERE id=?').run(diff, src.id);
    }

    // 账面仅按实际到货增减
    d.prepare('UPDATE batches SET quantity = quantity - ?, updated_at=CURRENT_TIMESTAMP WHERE id=?')
      .run(arrived, src.id);
    d.prepare('UPDATE batches SET quantity = quantity + ?, updated_at=CURRENT_TIMESTAMP WHERE id=?')
      .run(arrived, dest.id);

    d.prepare("UPDATE allocations SET arrived_qty=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?")
      .run(arrived, diff > 0 ? 'reconciling' : 'done', alloc.id);

    if (diff > 0) {
      d.prepare(`INSERT INTO reconciliation_items (allocation_no,batch_id,from_warehouse,to_warehouse,dispatch_qty,arrived_qty,diff_qty,status)
                 VALUES (?,?,?,?,?,?,?, 'pending')`)
        .run(allocationNo, src.id, alloc.from_warehouse, alloc.to_warehouse, alloc.dispatch_qty, arrived, diff);
    }

    // 腾出容量后尝试激活排队
    processQueueForBatch(src.id);

    return {
      allocation_no: allocationNo,
      dispatch_qty: alloc.dispatch_qty,
      arrived_qty: arrived,
      diff_qty: diff,
      status: diff > 0 ? 'reconciling' : 'done',
    };
  });
}

/* ----------------------------- 容量排队 ----------------------------- */

/** 处理某目的地批次上的排队（FIFO）。调用方需保证在事务内。 */
function processQueueForBatch(batchId) {
  const d = getDb();
  const batch = getBatchById(batchId);
  const items = d.prepare("SELECT * FROM capacity_queue WHERE batch_id=? AND status='waiting' ORDER BY id ASC")
    .all(batchId);
  let activated = 0;
  for (const item of items) {
    if (batch.status === 'frozen') break;
    const alloc = getAllocationByNo(item.allocation_no);
    if (!alloc || alloc.status !== 'queued') break;
    const src = getBatchById(alloc.batch_id);
    if (!src || src.status === 'frozen') break;
    const occ = d.prepare("SELECT allocation_no FROM batch_occupations WHERE batch_id=? AND status='occupied' LIMIT 1")
      .get(alloc.batch_id);
    if (occ) break; // 源批次仍被占用，跳过本批（FIFO 不插队）
    if (batch.quantity + item.qty > batch.capacity) break; // 容量仍满

    d.prepare("UPDATE allocations SET status='occupied', updated_at=CURRENT_TIMESTAMP WHERE id=?").run(alloc.id);
    d.prepare(`INSERT INTO batch_occupations (batch_id,allocation_no,qty,status) VALUES (?,?,?,'occupied')`)
      .run(alloc.batch_id, item.allocation_no, item.qty);
    d.prepare('UPDATE batches SET occupied_qty = occupied_qty + ?, updated_at=CURRENT_TIMESTAMP WHERE id=?')
      .run(item.qty, alloc.batch_id);
    d.prepare("UPDATE capacity_queue SET status='processed' WHERE id=?").run(item.id);
    activated++;
  }
  return activated;
}

function processQueue() {
  const d = getDb();
  const batchIds = d.prepare("SELECT DISTINCT batch_id FROM capacity_queue WHERE status='waiting'")
    .all()
    .map((r) => r.batch_id);
  let activated = 0;
  for (const id of batchIds) activated += tx(() => processQueueForBatch(id));
  return { activated };
}

/* ----------------------------- 温控 / 冻结 ----------------------------- */

function recordTemperature(warehouseId, batchNo, temperatureInput) {
  const temperature = Number(temperatureInput);
  if (Number.isNaN(temperature)) throw new BizError('温度值非法', 400);
  return tx(() => {
    const d = getDb();
    const batch = getBatch(warehouseId, batchNo);
    if (!batch) throw new BizError(`批次不存在: ${warehouseId}/${batchNo}`, 404);
    const within = temperature >= batch.temp_min && temperature <= batch.temp_max;
    d.prepare('INSERT INTO temperature_logs (batch_id,temperature,within_limit) VALUES (?,?,?)')
      .run(batch.id, temperature, within ? 1 : 0);
    if (!within) {
      // 温度超限过的库存自动冻结（粘滞：即使恢复正常也保持冻结）
      d.prepare("UPDATE batches SET temp_exceeded=1, status='frozen', updated_at=CURRENT_TIMESTAMP WHERE id=?")
        .run(batch.id);
    }
    const after = getBatch(warehouseId, batchNo);
    return {
      batch_no: batchNo,
      warehouse_id: warehouseId,
      temperature,
      within_limit: within,
      frozen: after.status === 'frozen',
      temp_exceeded: after.temp_exceeded === 1,
      available: availableQty(after),
    };
  });
}

/* ----------------------------- 车厢断网恢复上报（幂等合并） ----------------------------- */

function submitTransportReports(carriageNo, reports) {
  if (!carriageNo) throw new BizError('carriage_no 必填', 400);
  if (!Array.isArray(reports)) throw new BizError('reports 必须是数组', 400);
  const results = [];
  let inserted = 0;
  let duplicates = 0;
  for (const r of reports) {
    const res = processOneReport(carriageNo, r);
    results.push(res);
    if (res.duplicate) duplicates++; else inserted++;
  }
  return { carriage_no: carriageNo, received: reports.length, inserted, duplicates, results };
}

function processOneReport(carriageNo, r) {
  if (!r || !r.record_no || !r.event_type) {
    return { record_no: r && r.record_no, duplicate: false, applied: false, error: 'record_no / event_type 必填' };
  }
  const d = getDb();
  // 幂等占位（独立事务）：同一条记录重复上报只入库一次
  const claimed = tx(() => {
    const dup = d.prepare('SELECT id FROM transport_reports WHERE record_no=?').get(r.record_no);
    if (dup) return { duplicate: true };
    d.prepare(`INSERT INTO transport_reports (record_no,allocation_no,carriage_no,event_type,payload,merged)
               VALUES (?,?,?,?,?,1)`)
      .run(r.record_no, r.allocation_no || null, carriageNo, r.event_type, JSON.stringify(r));
    return { duplicate: false };
  });
  if (claimed.duplicate) return { record_no: r.record_no, duplicate: true, applied: false };

  // 应用事件（各自独立事务，避免嵌套）；业务失败不影响上报记录已入库
  let applied = false;
  let error = null;
  let result = null;
  try {
    if (r.event_type === 'dispatch') {
      result = submitAllocation({
        allocation_no: r.allocation_no,
        batch_no: r.batch_no,
        from_warehouse: r.from_warehouse,
        to_warehouse: r.to_warehouse,
        qty: r.qty,
      });
    } else if (r.event_type === 'arrive') {
      result = arriveAllocation(r.allocation_no, r.arrived_qty);
    } else if (r.event_type === 'temperature') {
      result = recordTemperature(r.warehouse_id, r.batch_no, r.temperature);
    } else {
      error = `未知事件类型: ${r.event_type}`;
    }
    applied = !error;
  } catch (e) {
    error = e.message;
  }
  return { record_no: r.record_no, duplicate: false, applied, error, result };
}

/* ----------------------------- 期初回填 ----------------------------- */

function backfillOpening() {
  return tx(() => {
    const d = getDb();
    // 缺调拨记录的批次（既非调出方也非调入方），按现存数量回填期初
    const rows = d.prepare(`
      SELECT b.* FROM batches b
      WHERE b.opening_backfilled = 0
        AND NOT EXISTS (
          SELECT 1 FROM allocations a WHERE a.batch_id = b.id OR a.destination_batch_id = b.id
        )
    `).all();
    const upd = d.prepare("UPDATE batches SET opening_qty=quantity, opening_backfilled=1, updated_at=CURRENT_TIMESTAMP WHERE id=?");
    for (const b of rows) upd.run(b.id);
    return {
      backfilled: rows.length,
      items: rows.map((b) => ({
        batch_no: b.batch_no,
        warehouse_id: b.warehouse_id,
        opening_qty: b.quantity,
      })),
    };
  });
}

module.exports = {
  initDb,
  BizError,
  // 批次
  createBatch,
  listBatches,
  listTemperatureLogs,
  // 调拨
  submitAllocation,
  arriveAllocation,
  listAllocations,
  // 排队
  processQueue,
  listQueue,
  // 温控
  recordTemperature,
  // 上报
  submitTransportReports,
  // 对账
  listReconciliation,
  // 期初
  backfillOpening,
};
