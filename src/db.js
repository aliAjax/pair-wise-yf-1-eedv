const Database = require('better-sqlite3');
const fs = require('fs');
const path = require('path');

let db;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no TEXT NOT NULL,
  drug_name TEXT NOT NULL,
  warehouse_id TEXT NOT NULL,
  quantity REAL NOT NULL DEFAULT 0,
  occupied_qty REAL NOT NULL DEFAULT 0,
  capacity REAL NOT NULL DEFAULT 1000,
  temp_min REAL NOT NULL DEFAULT 2,
  temp_max REAL NOT NULL DEFAULT 8,
  temp_exceeded INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'normal',
  opening_qty REAL,
  opening_backfilled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(warehouse_id, batch_no)
);

CREATE TABLE IF NOT EXISTS allocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  allocation_no TEXT NOT NULL UNIQUE,
  batch_id INTEGER NOT NULL,
  destination_batch_id INTEGER,
  from_warehouse TEXT NOT NULL,
  to_warehouse TEXT NOT NULL,
  dispatch_qty REAL NOT NULL,
  arrived_qty REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'occupied',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (batch_id) REFERENCES batches(id)
);

CREATE TABLE IF NOT EXISTS batch_occupations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  allocation_no TEXT NOT NULL,
  qty REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'occupied',
  occupied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  released_at TEXT,
  FOREIGN KEY (batch_id) REFERENCES batches(id)
);
CREATE INDEX IF NOT EXISTS idx_occ_batch ON batch_occupations(batch_id, status);

CREATE TABLE IF NOT EXISTS transport_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_no TEXT NOT NULL UNIQUE,
  allocation_no TEXT,
  carriage_no TEXT,
  event_type TEXT NOT NULL,
  payload TEXT,
  merged INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reconciliation_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  allocation_no TEXT NOT NULL,
  batch_id INTEGER NOT NULL,
  from_warehouse TEXT NOT NULL,
  to_warehouse TEXT NOT NULL,
  dispatch_qty REAL NOT NULL,
  arrived_qty REAL NOT NULL,
  diff_qty REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS capacity_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  allocation_no TEXT NOT NULL,
  qty REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'waiting',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS temperature_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  temperature REAL NOT NULL,
  within_limit INTEGER NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
`;

function initDb(dbPath) {
  if (db) return db;
  const file = dbPath || process.env.DB_PATH || path.join(__dirname, '..', 'data', 'allocation.db');
  if (file !== ':memory:') {
    fs.mkdirSync(path.dirname(file), { recursive: true });
  }
  db = new Database(file);
  db.pragma('journal_mode = WAL');
  db.pragma('busy_timeout = 8000');
  db.pragma('foreign_keys = ON');
  db.exec(SCHEMA);
  return db;
}

function getDb() {
  if (!db) initDb();
  return db;
}

module.exports = { initDb, getDb };
