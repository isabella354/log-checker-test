from pathlib import Path

import numpy as np
import pytest

from robotlogs.alerts import check_anomalies
from robotlogs.config import load_config
from robotlogs.db import Database
from robotlogs.ingest import process_log
from robotlogs.metrics import Series, enabled_intervals, make_grid
from robotlogs.parse import list_signals, read_wpilog

from .wpilog_writer import WpilogWriter

CONFIG = """
team: 1234
local_log_dir: ./logs
db_path: ./test.db
signal_map:
  battery_voltage: /Batt/V
  total_current: /PD/Total
  brownout: /Sys/Brownout
  battery_id: NT:/SmartDashboard/BatteryID
  motors:
    arm:
      supply: /Arm/Supply
      stator: /Arm/Stator
      temp: /Arm/Temp
"""


def write_match(path: Path, *, match_number=1, epoch_s=1_760_000_000, arm_stator=40.0, sag=0.02):
    """10 s disabled, 20 s enabled at 50 Hz, 5 s disabled."""
    w = WpilogWriter(path)
    w.append("systemTime", "int64", 1.0, int(epoch_s * 1e6))
    w.append("NT:/FMSInfo/EventName", "string", 1.0, "CASJ")
    w.append("NT:/FMSInfo/MatchType", "int64", 1.0, 2)
    w.append("NT:/FMSInfo/MatchNumber", "int64", 1.0, match_number)
    w.append("NT:/SmartDashboard/BatteryID", "string", 1.0, "B07")
    w.append("DS:enabled", "boolean", 0.0, False)
    w.append("DS:enabled", "boolean", 10.0, True)
    w.append("DS:enabled", "boolean", 30.0, False)
    w.append("/Sys/Brownout", "boolean", 0.0, False)
    w.append("/Sys/Brownout", "boolean", 15.0, True)
    w.append("/Sys/Brownout", "boolean", 15.1, False)
    for t in np.arange(0, 35, 0.02):
        enabled = 10 <= t < 30
        amps = 100.0 * (1 + np.sin(t)) if enabled else 2.0
        w.append("/PD/Total", "double", t, amps)
        w.append("/Batt/V", "double", t, 12.6 - sag * amps)
        w.append("/Arm/Supply", "double", t, arm_stator / 4 if enabled else 0.0)
        w.append("/Arm/Stator", "double", t, arm_stator if enabled else 0.0)
        w.append("/Arm/Temp", "double", t, 30 + t)
    w.close()


@pytest.fixture
def env(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.local_log_dir.mkdir()
    return cfg, Database(cfg.db_path)


def test_enabled_intervals_and_grid():
    en = Series(np.array([0, 5, 10, 12]), np.array([0, 1, 0, 1]))
    iv = enabled_intervals(en, 0, 15)
    assert iv == [(5, 10), (12, 15)]
    assert make_grid(iv, 0.5).size == 10 + 6


def test_reader_round_trip(env, tmp_path):
    p = tmp_path / "m.wpilog"
    write_match(p)
    names = {n for n, _, _ in list_signals(p)}
    assert {"DS:enabled", "/Batt/V", "NT:/FMSInfo/EventName"} <= names
    s = read_wpilog(p)
    assert s.last_string("NT:/FMSInfo/EventName") == "CASJ"
    assert len(s.numeric["/Batt/V"].t) == 1750


def test_ingest_one_match(env):
    cfg, db = env
    p = cfg.local_log_dir / "FRC_q1.wpilog"
    write_match(p, sag=0.02)
    log_id = process_log(p, cfg, db)
    row = db.log_row(log_id)

    assert row["event"] == "CASJ" and row["match_type"] == "qualification" and row["match_number"] == 1
    assert row["battery_id"] == "B07"
    assert row["enabled_s"] == pytest.approx(20.0, abs=0.05)
    assert row["sag_ohms"] == pytest.approx(0.02, rel=1e-3)
    assert row["brownouts"] == 1
    assert row["peak_current"] == pytest.approx(200, abs=1)
    assert row["start_voltage"] == pytest.approx(12.6 - 0.02 * 2.0, abs=1e-6)  # last disabled sample
    # energy = mean(V*I) * 20 s / 3600
    assert 5 < row["energy_wh"] < 15
    assert row["started_at"].startswith("2025-10-09")

    m = db.conn.execute("SELECT * FROM motor_stats WHERE log_id=?", (log_id,)).fetchone()
    assert m["stator_supply_ratio"] == pytest.approx(4.0)
    assert m["peak_temp"] == pytest.approx(30 + 30, abs=0.05)
    assert m["time_hot"] == 0

    assert process_log(p, cfg, db) is None  # dedupe by hash


def test_wear_alert_fires(env):
    cfg, db = env
    last = None
    for k in range(12):
        p = cfg.local_log_dir / f"m{k}.wpilog"
        write_match(p, match_number=k + 1, epoch_s=1_760_000_000 + k * 600, arm_stator=40 * (1.05 ** k))
        last = process_log(p, cfg, db)
    msgs = check_anomalies(db, last, cfg)
    assert any("rising" in m and "arm" in m for m in msgs), msgs


def test_power_metrics(env):
    cfg, db = env
    p = cfg.local_log_dir / "m.wpilog"
    write_match(p, sag=0.02)
    row = db.log_row(process_log(p, cfg, db))
    # avg power * enabled time == energy
    assert row["avg_power_w"] * row["enabled_s"] / 3600 == pytest.approx(row["energy_wh"], rel=1e-6)
    assert row["peak_power_w"] == pytest.approx(200 * (12.6 - 0.02 * 200), rel=0.01)


def test_compute_trend():
    from robotlogs.trends import compute_trend

    assert compute_trend("x", "", [1, 2, 3], True) is None  # too few matches
    rising = compute_trend("x", "", [10, 10, 11, 12, 13, 14, 15, 16], higher_is_worse=True)
    assert rising.verdict == "worse" and rising.per_match > 0
    assert compute_trend("v", "V", [12.5, 12.4, 12.0, 11.9], higher_is_worse=False).verdict == "steady"
    assert compute_trend("v", "V", [12, 12, 10, 10], higher_is_worse=False).verdict == "worse"
    assert compute_trend("b", "", [0, 0, 1, 2], higher_is_worse=True).verdict == "worse"  # from zero


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, file_name TEXT, sha256 TEXT UNIQUE, "
                 "ingested_at TEXT, started_at TEXT, energy_wh REAL)")
    conn.commit()
    conn.close()
    db = Database(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(logs)")}
    assert {"avg_power_w", "peak_power_w", "sag_ohms"} <= cols


def test_dashboard_renders(env, monkeypatch):
    from streamlit.testing.v1 import AppTest

    cfg, db = env
    for k in range(8):
        p = cfg.local_log_dir / f"m{k}.wpilog"
        write_match(p, match_number=k + 1, epoch_s=1_760_000_000 + k * 600, arm_stator=40 + 2 * k)
        process_log(p, cfg, db)
    app = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    monkeypatch.setattr("sys.argv", ["app.py", "--config", str(cfg.db_path.parent / "config.yaml")])
    at = AppTest.from_file(str(app), default_timeout=30).run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 6
    assert any("CASJ Q8" in m.value for m in at.subheader)


def test_team_config_reads_advantagekit_log(tmp_path):
    """Write a log shaped like Team3256's AdvantageKit output and ingest it with the real config.yaml."""
    import shutil

    repo = Path(__file__).resolve().parent.parent
    shutil.copy(repo / "config.yaml", tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    cfg.local_log_dir.mkdir()
    db = Database(cfg.db_path)

    p = cfg.local_log_dir / "akit_26-03-07_18-30-12_casj_q14.wpilog"
    w = WpilogWriter(p)
    w.append("/SystemStats/EpochTimeMicros", "int64", 0.5, 0)  # clock not valid yet
    w.append("/SystemStats/EpochTimeMicros", "int64", 2.0, int(1_772_900_000 * 1e6))
    w.append("/DriverStation/EventName", "string", 1.0, "CASJ")
    w.append("/DriverStation/MatchType", "int64", 1.0, 2)
    w.append("/DriverStation/MatchNumber", "int64", 1.0, 14)
    w.append("/DriverStation/Enabled", "boolean", 0.0, False)
    w.append("/DriverStation/Enabled", "boolean", 5.0, True)
    w.append("/DriverStation/Enabled", "boolean", 25.0, False)
    w.append("/SystemStats/BrownedOut", "boolean", 0.0, False)
    for t in np.arange(0, 30, 0.02):
        on = 5 <= t < 25
        w.append("/SystemStats/BatteryVoltage", "double", t, 11.0 if on else 12.5)
        w.append("/Shooter/ShooterMotorSupplyCurrents", "double[]", t, [20.0, 21.0, 22.0, 23.0] if on else [0.0] * 4)
        w.append("/Shooter/ShooterMotorStatorCurrents", "double[]", t, [30.0, 31.0, 32.0, 33.0] if on else [0.0] * 4)
        w.append("/Shooter/ShooterMotorTemperatures", "double[]", t, [40.0, 41.0, 42.0, 43.0])
        w.append("/feeder/FeederMotorLeftSupplyCurrent", "double", t, 5.0 if on else 0.0)
        w.append("/feeder/FeederMotorLeftStatorCurrent", "double", t, 15.0 if on else 0.0)
        w.append("/LinearSlide/LeftMotorStatorCurrent", "double", t, 8.0 if on else 0.0)
    w.close()

    log_id = process_log(p, cfg, db)
    row = db.log_row(log_id)
    assert (row["event"], row["match_type"], row["match_number"]) == ("CASJ", "qualification", 14)
    assert row["started_at"].startswith("2026-03-07")
    assert row["enabled_s"] == pytest.approx(20.0, abs=0.05)
    assert row["min_voltage"] == pytest.approx(11.0)
    # no PowerDistribution logged -> total current = sum of tracked supply currents
    assert row["avg_current"] == pytest.approx(20 + 21 + 22 + 23 + 5)

    motors = {r["motor"]: r for r in db.conn.execute("SELECT * FROM motor_stats WHERE log_id=?", (log_id,))}
    assert motors["shooter_3"]["avg_supply"] == pytest.approx(22.0)
    assert motors["shooter_3"]["avg_stator"] == pytest.approx(32.0)
    assert motors["shooter_4"]["peak_temp"] == pytest.approx(43.0)
    assert motors["feeder_left"]["stator_supply_ratio"] == pytest.approx(3.0)
    assert motors["slide_left"]["avg_stator"] == pytest.approx(8.0)
    assert "indexer_left" not in motors  # not in this log -> skipped, not an error
