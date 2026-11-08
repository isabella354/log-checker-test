"""SQLite storage: one row per log/match, one row per motor per match."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
  remote_name   TEXT NOT NULL,
  remote_size   INTEGER NOT NULL,
  downloaded_at TEXT NOT NULL,
  PRIMARY KEY (remote_name, remote_size)
);

CREATE TABLE IF NOT EXISTS skipped (
  sha256     TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  reason     TEXT,
  skipped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
  id                INTEGER PRIMARY KEY,
  file_name         TEXT NOT NULL,
  sha256            TEXT NOT NULL UNIQUE,
  ingested_at       TEXT NOT NULL,
  started_at        TEXT,            -- wall clock from the log's systemTime, else file mtime
  event             TEXT,
  match_type        TEXT,
  match_number      INTEGER,
  battery_id        TEXT,
  enabled_s         REAL,
  enable_drops      INTEGER,
  start_voltage     REAL,
  min_voltage       REAL,
  avg_voltage       REAL,
  time_below_low_v  REAL,
  time_below_crit_v REAL,
  brownouts         INTEGER,
  peak_current      REAL,
  avg_current       REAL,
  energy_wh         REAL,
  avg_power_w       REAL,
  peak_power_w      REAL,
  sag_ohms          REAL
);

CREATE TABLE IF NOT EXISTS motor_stats (
  log_id              INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
  motor               TEXT NOT NULL,
  peak_supply         REAL,
  avg_supply          REAL,
  peak_stator         REAL,
  avg_stator          REAL,
  stator_supply_ratio REAL,
  peak_temp           REAL,
  time_hot            REAL,
  PRIMARY KEY (log_id, motor)
);
"""

LOG_METRICS = [
    "enabled_s", "enable_drops", "start_voltage", "min_voltage", "avg_voltage", "time_below_low_v",
    "time_below_crit_v", "brownouts", "peak_current", "avg_current", "energy_wh",
    "avg_power_w", "peak_power_w", "sag_ohms",
]
MOTOR_METRICS = [
    "peak_supply", "avg_supply", "peak_stator", "avg_stator",
    "stator_supply_ratio", "peak_temp", "time_hot",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Databases created by an older version lack newer metric columns; add them
        (old matches get NULL until re-ingested)."""
        for table, cols in (("logs", LOG_METRICS), ("motor_stats", MOTOR_METRICS)):
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
        self.conn.commit()

    # ---- dedupe ----
    def was_downloaded(self, name: str, size: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM downloads WHERE remote_name=? AND remote_size=?", (name, size)
        ).fetchone() is not None

    def mark_downloaded(self, name: str, size: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO downloads VALUES (?, ?, ?)", (name, size, _now())
            )

    def is_ingested(self, sha256: str) -> bool:
        """True if already in logs, or previously skipped (e.g. robot never enabled)."""
        return self.conn.execute(
            "SELECT 1 FROM logs WHERE sha256=? UNION SELECT 1 FROM skipped WHERE sha256=?", (sha256, sha256)
        ).fetchone() is not None

    def mark_skipped(self, name: str, sha256: str, reason: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO skipped VALUES (?, ?, ?, ?)", (sha256, name, reason, _now()))

    # ---- writes ----
    def insert_log(self, file_name: str, sha256: str, info: dict, summary: dict, motors: list[dict]) -> int:
        cols = ["file_name", "sha256", "ingested_at", "started_at", "event", "match_type",
                "match_number", "battery_id", *LOG_METRICS]
        vals = [file_name, sha256, _now(), info.get("started_at"), info.get("event"),
                info.get("match_type"), info.get("match_number"), info.get("battery_id"),
                *(summary.get(k) for k in LOG_METRICS)]
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO logs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
            )
            log_id = cur.lastrowid
            mcols = ["log_id", "motor", *MOTOR_METRICS]
            self.conn.executemany(
                f"INSERT INTO motor_stats ({', '.join(mcols)}) VALUES ({', '.join('?' * len(mcols))})",
                [[log_id, m["motor"], *(m.get(k) for k in MOTOR_METRICS)] for m in motors],
            )
        return log_id

    # ---- reads for alerts ----
    def log_row(self, log_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM logs WHERE id=?", (log_id,)).fetchone()

    def history(self, metric: str, before_log_id: int, n: int) -> list[float]:
        assert metric in LOG_METRICS
        cur = self.log_row(before_log_id)
        rows = self.conn.execute(
            f"""SELECT {metric} FROM logs
                WHERE id != ? AND {metric} IS NOT NULL AND COALESCE(started_at, '') <= COALESCE(?, '')
                ORDER BY started_at DESC, id DESC LIMIT ?""",
            (before_log_id, cur["started_at"], n),
        ).fetchall()
        return [r[0] for r in rows]

    def motor_history(self, motor: str, metric: str, up_to_log_id: int, n: int) -> list[float]:
        """Oldest-first values for one motor, ending with (and including) up_to_log_id."""
        assert metric in MOTOR_METRICS
        cur = self.log_row(up_to_log_id)
        rows = self.conn.execute(
            f"""SELECT ms.{metric} FROM motor_stats ms JOIN logs l ON l.id = ms.log_id
                WHERE ms.motor = ? AND ms.{metric} IS NOT NULL
                  AND COALESCE(l.started_at, '') <= COALESCE(?, '')
                ORDER BY l.started_at DESC, l.id DESC LIMIT ?""",
            (motor, cur["started_at"], n),
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def motor_value(self, log_id: int, motor: str, metric: str) -> float | None:
        assert metric in MOTOR_METRICS
        r = self.conn.execute(
            f"SELECT {metric} FROM motor_stats WHERE log_id=? AND motor=?", (log_id, motor)
        ).fetchone()
        return r[0] if r else None

    def motors_in(self, log_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT motor FROM motor_stats WHERE log_id=?", (log_id,))]

    def recent_log_ids(self, n: int) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM logs ORDER BY started_at DESC, id DESC LIMIT ?", (n,)
        )]
