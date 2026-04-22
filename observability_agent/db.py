"""Database initialisation and helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_FILENAME = "observability.db"


def get_db_path() -> str:
    return str(Path.cwd() / DB_FILENAME)


def init_db(db_path: str | None = None) -> None:
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL    NOT NULL,
                name      TEXT    NOT NULL,
                value     REAL    NOT NULL,
                service   TEXT    NOT NULL,
                labels    TEXT    DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_snt ON metrics(service, name, timestamp)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                level     TEXT NOT NULL,
                service   TEXT NOT NULL,
                message   TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts  ON logs(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_svc ON logs(service, timestamp)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_state (
                id                  INTEGER PRIMARY KEY CHECK (id = 1),
                panels              TEXT    DEFAULT '["overview","timeseries","histogram","logs"]',
                timeseries_metric   TEXT    DEFAULT 'latency_p99',
                timeseries_service  TEXT    DEFAULT 'all',
                histogram_metric    TEXT    DEFAULT 'latency_p99',
                histogram_service   TEXT    DEFAULT 'all',
                log_level           TEXT    DEFAULT 'all',
                log_keyword         TEXT    DEFAULT '',
                log_service         TEXT    DEFAULT 'all',
                time_range_minutes  INTEGER DEFAULT 30,
                agent_status        TEXT    DEFAULT 'idle',
                agent_last_action   TEXT    DEFAULT '',
                terminal_width      INTEGER DEFAULT 0,
                terminal_height     INTEGER DEFAULT 0,
                updated_at          REAL    DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO dashboard_state (id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()
