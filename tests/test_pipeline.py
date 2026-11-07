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
