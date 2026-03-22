import sqlite3
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pcrent.db"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
lock = threading.Lock()


def _column_names(table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _ensure_machine_columns() -> None:
    machine_columns = _column_names("machines")
    if "machine_key" not in machine_columns:
        conn.execute("ALTER TABLE machines ADD COLUMN machine_key TEXT")
    if "last_seen_at" not in machine_columns:
        conn.execute("ALTER TABLE machines ADD COLUMN last_seen_at TEXT")
    if "os_version" not in machine_columns:
        conn.execute("ALTER TABLE machines ADD COLUMN os_version TEXT")
    if "nvidia_driver" not in machine_columns:
        conn.execute("ALTER TABLE machines ADD COLUMN nvidia_driver TEXT")

    # Unique per physical machine when machine_key is provided.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_key_unique
        ON machines(machine_key)
        WHERE machine_key IS NOT NULL AND machine_key != ''
        """
    )


def init_db() -> None:
    with lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS machines (
              id TEXT PRIMARY KEY,
              machine_key TEXT,
              gpu_model TEXT NOT NULL,
              gpu_vram_gb REAL NOT NULL,
              cpu_cores INTEGER NOT NULL,
              ram_gb REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'idle',
              registered_at TEXT NOT NULL,
              last_seen_at TEXT,
              os_version TEXT,
              nvidia_driver TEXT
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
            """
        )
        _ensure_machine_columns()
        conn.commit()
