const Database = require('better-sqlite3');
const path = require('path');

const db = new Database(path.join(__dirname, 'pcrent.db'));

db.exec(`
  CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    gpu_model TEXT NOT NULL,
    gpu_vram_gb REAL NOT NULL,
    cpu_cores INTEGER NOT NULL,
    ram_gb REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    registered_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    machine_id TEXT NOT NULL,
    input_filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    output_files TEXT NOT NULL DEFAULT '[]',
    submitted_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(id)
  );
`);

module.exports = db;
