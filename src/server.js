const express = require('express');
const svc = require('./service');

const app = express();
app.use(express.json());

// 批次
app.post('/api/batches', (req, res, next) => {
  try { res.status(201).json(svc.createBatch(req.body)); } catch (e) { next(e); }
});
app.get('/api/batches', (req, res, next) => {
  try { res.json(svc.listBatches()); } catch (e) { next(e); }
});
app.get('/api/temperature/logs', (req, res, next) => {
  try { res.json(svc.listTemperatureLogs()); } catch (e) { next(e); }
});

// 调拨单
app.post('/api/allocations', (req, res, next) => {
  try { res.status(201).json(svc.submitAllocation(req.body)); } catch (e) { next(e); }
});
app.get('/api/allocations', (req, res, next) => {
  try { res.json(svc.listAllocations()); } catch (e) { next(e); }
});
app.post('/api/allocations/:no/arrive', (req, res, next) => {
  try { res.json(svc.arriveAllocation(req.params.no, req.body.arrived_qty)); } catch (e) { next(e); }
});

// 温控
app.post('/api/temperature', (req, res, next) => {
  try {
    res.json(svc.recordTemperature(req.body.warehouse_id, req.body.batch_no, req.body.temperature));
  } catch (e) { next(e); }
});

// 车厢断网恢复上报（幂等合并）
app.post('/api/transport/reports', (req, res, next) => {
  try {
    res.json(svc.submitTransportReports(req.body.carriage_no, req.body.reports));
  } catch (e) { next(e); }
});

// 对账 / 排队
app.get('/api/reconciliation', (req, res, next) => {
  try { res.json(svc.listReconciliation()); } catch (e) { next(e); }
});
app.get('/api/queue', (req, res, next) => {
  try { res.json(svc.listQueue()); } catch (e) { next(e); }
});
app.post('/api/admin/process-queue', (req, res, next) => {
  try { res.json(svc.processQueue()); } catch (e) { next(e); }
});

// 期初回填
app.post('/api/admin/backfill-opening', (req, res, next) => {
  try { res.json(svc.backfillOpening()); } catch (e) { next(e); }
});

// 业务错误处理
app.use((err, req, res, _next) => {
  const status = err.status || 500;
  res.status(status).json({ error: err.message, ...(err.extra || {}) });
});

const port = process.env.PORT || 3000;
if (require.main === module) {
  svc.initDb();
  app.listen(port, () => {
    console.log(`冷链调拨核对接口 listening on :${port}`);
  });
}

module.exports = app;
