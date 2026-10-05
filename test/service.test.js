const test = require('node:test');
const assert = require('node:assert');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');
const svc = require('../src/service');

let dbPath;
test.before(() => {
  dbPath = path.join(os.tmpdir(), `alloc-test-${Date.now()}-${Math.random().toString(36).slice(2)}.db`);
  svc.initDb(dbPath);
});
test.after(() => {
  try { fs.unlinkSync(dbPath); } catch (_) { /* ignore */ }
});

test('1. 两仓库同时提交：后到者确认批次占用，未释放被挡回并告知当前单号', () => {
  svc.createBatch({ batch_no: 'B001', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 100 });

  const first = svc.submitAllocation({
    allocation_no: 'A001', batch_no: 'B001', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 30,
  });
  assert.equal(first.queued, false);

  const src = svc.listBatches().find((b) => b.warehouse_id === 'WH-A' && b.batch_no === 'B001');
  assert.equal(src.occupied_qty, 30);
  assert.equal(src.available, 70); // 可放量 = 余量 - 在途

  assert.throws(
    () => svc.submitAllocation({
      allocation_no: 'A002', batch_no: 'B001', from_warehouse: 'WH-A', to_warehouse: 'WH-C', qty: 10,
    }),
    (e) => {
      assert.equal(e.status, 409);
      assert.match(e.message, /批次占用中，当前单号: A001/);
      assert.equal(e.extra.current_allocation_no, 'A001');
      return true;
    },
  );
});

test('2. 可放量不足时拒绝调拨', () => {
  svc.createBatch({ batch_no: 'B002', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 10 });
  assert.throws(
    () => svc.submitAllocation({
      allocation_no: 'A003', batch_no: 'B002', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 20,
    }),
    (e) => e.status === 409 && /可放量不足/.test(e.message),
  );
});

test('3. 到货少于发出：差额挂跨仓对账待处理，批次余量不跟着改', () => {
  svc.createBatch({ batch_no: 'B003', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 100 });
  svc.submitAllocation({
    allocation_no: 'A004', batch_no: 'B003', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 50,
  });

  const r = svc.arriveAllocation('A004', 45);
  assert.equal(r.diff_qty, 5);
  assert.equal(r.status, 'reconciling');

  const src = svc.listBatches().find((b) => b.warehouse_id === 'WH-A' && b.batch_no === 'B003');
  assert.equal(src.quantity, 55);     // 仅按实际到货减少
  assert.equal(src.occupied_qty, 5);  // 差额仍挂在途，不冲减余量
  assert.equal(src.available, 50);

  const dest = svc.listBatches().find((b) => b.warehouse_id === 'WH-B' && b.batch_no === 'B003');
  assert.equal(dest.quantity, 45);

  const rec = svc.listReconciliation();
  assert.equal(rec.length, 1);
  assert.equal(rec[0].diff_qty, 5);
  assert.equal(rec[0].dispatch_qty, 50);
  assert.equal(rec[0].arrived_qty, 45);
  assert.equal(rec[0].status, 'pending');
});

test('4. 批次容量满了排队，腾出容量后按 FIFO 激活', () => {
  svc.createBatch({ batch_no: 'B004', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 100, capacity: 100 });
  svc.createBatch({ batch_no: 'B004', drug_name: '疫苗', warehouse_id: 'WH-B', quantity: 90, capacity: 100 });

  // WH-B 容量 100 已存 90，再收 15 超出 → 排队
  const r = svc.submitAllocation({
    allocation_no: 'A005', batch_no: 'B004', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 15,
  });
  assert.equal(r.queued, true);
  assert.equal(r.queue_position, 1);
  assert.equal(svc.listQueue().length, 1);

  // WH-B 调出 20 腾出容量
  svc.submitAllocation({
    allocation_no: 'A006', batch_no: 'B004', from_warehouse: 'WH-B', to_warehouse: 'WH-C', qty: 20,
  });
  svc.arriveAllocation('A006', 20);

  // WH-B 现存 70，容量 100，可收 30 ≥ 15 → 排队激活
  const q = svc.listQueue().find((x) => x.allocation_no === 'A005');
  assert.equal(q.status, 'processed');

  const a005 = svc.listAllocations().find((x) => x.allocation_no === 'A005');
  assert.equal(a005.status, 'occupied');
});

test('5. 温控变化重算可放量；温度超限过的库存自动冻结且恢复后仍冻结', () => {
  svc.createBatch({ batch_no: 'B005', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 100, temp_min: 2, temp_max: 8 });

  const r1 = svc.recordTemperature('WH-A', 'B005', 5);
  assert.equal(r1.within_limit, true);
  assert.equal(r1.frozen, false);
  assert.equal(r1.available, 100);

  const r2 = svc.recordTemperature('WH-A', 'B005', 10);
  assert.equal(r2.within_limit, false);
  assert.equal(r2.frozen, true);
  assert.equal(r2.temp_exceeded, true);
  assert.equal(r2.available, 0); // 冻结后可放量归零

  // 恢复正常温度，仍保持冻结
  const r3 = svc.recordTemperature('WH-A', 'B005', 5);
  assert.equal(r3.frozen, true);
  assert.equal(r3.available, 0);

  // 冻结后禁止调拨
  assert.throws(
    () => svc.submitAllocation({
      allocation_no: 'A007', batch_no: 'B005', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 1,
    }),
    (e) => e.status === 409 && /冻结/.test(e.message),
  );
});

test('6. 旧数据缺调拨记录的批次按现存数量回填期初', () => {
  svc.createBatch({ batch_no: 'B006', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 77 });
  svc.createBatch({ batch_no: 'B007', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 50 });
  svc.submitAllocation({
    allocation_no: 'A008', batch_no: 'B007', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 10,
  });

  const r = svc.backfillOpening();
  const b006 = r.items.find((i) => i.batch_no === 'B006');
  assert.equal(b006.opening_qty, 77);
  assert.ok(!r.items.find((i) => i.batch_no === 'B007')); // 有调拨记录，不回填

  // 幂等：再次回填为 0
  const r2 = svc.backfillOpening();
  assert.equal(r2.backfilled, 0);
});

test('7. 车厢断网恢复上报：合并入库，同一条记录重复上报只入库一次', () => {
  svc.createBatch({ batch_no: 'B008', drug_name: '疫苗', warehouse_id: 'WH-A', quantity: 100 });
  const reports = [
    { record_no: 'R001', event_type: 'dispatch', allocation_no: 'A009', batch_no: 'B008', from_warehouse: 'WH-A', to_warehouse: 'WH-B', qty: 20 },
    { record_no: 'R002', event_type: 'arrive', allocation_no: 'A009', arrived_qty: 20 },
    { record_no: 'R003', event_type: 'temperature', warehouse_id: 'WH-A', batch_no: 'B008', temperature: 6 },
  ];

  const r1 = svc.submitTransportReports('CAR-01', reports);
  assert.equal(r1.inserted, 3);
  assert.equal(r1.duplicates, 0);

  // 断网恢复后重复上报同一批
  const r2 = svc.submitTransportReports('CAR-01', reports);
  assert.equal(r2.inserted, 0);
  assert.equal(r2.duplicates, 3);

  // 调拨只入库一次
  const alloc = svc.listAllocations().find((a) => a.allocation_no === 'A009');
  assert.equal(alloc.status, 'done');
  const b = svc.listBatches().find((x) => x.warehouse_id === 'WH-A' && x.batch_no === 'B008');
  assert.equal(b.quantity, 80);
});
