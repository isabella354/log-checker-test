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


def test_team_config_reads_ctre_session(tmp_path, monkeypatch):
    """A CTRE session folder (one .hoot per CAN bus) ingested with the real config.yaml.
    owlet is stubbed out: the fake .hoot files already contain wpilog data in owlet's naming."""
    import shutil

    import robotlogs.parse as parse
    from robotlogs.metrics import count_enable_drops

    repo = Path(__file__).resolve().parent.parent
    shutil.copy(repo / "config.yaml", tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    db = Database(cfg.db_path)

    def fake_convert(path, out_dir, owlet_dirs, owlet_args):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (path.stem + ".wpilog")
        shutil.copy(path, out)
        return out
    monkeypatch.setattr(parse, "convert_hoot", fake_convert)

    sess = cfg.local_log_dir / "mttd_Q12"
    sess.mkdir(parents=True)
    rio = WpilogWriter(sess / "mttd_Q12_rio_2026-11-07_18-07-16.hoot")
    can = WpilogWriter(sess / "mttd_Q12_138A862350374E53202020472B3810FF_2026-11-07_18-07-16.hoot")
    rio.append("MatchType", "string", 1.0, "None")      # what our robot code actually logs
    rio.append("MatchNumber", "string", 1.0, "0")
    rio.append("EventName", "string", 1.0, "")
    # enabled 10-30 s, with a 0.2 s blip at 20 s (counts as a drop)
    for t, on in [(0, False), (10, True), (20, False), (20.2, True), (30, False)]:
        rio.append("RobotEnable", "boolean", t, on)
    for t in np.arange(0, 35, 0.02):
        on = 10 <= t < 30
        rio.append("Phoenix6/TalonFX-44/SupplyCurrent", "double", t, 10.0 if on else 0.0)
        rio.append("Phoenix6/TalonFX-44/StatorCurrent", "double", t, 20.0 if on else 0.0)
        # TalonFX-0 exists on both buses on our robot; it must not collide
        rio.append("Phoenix6/TalonFX-0/SupplyCurrent", "double", t, 999.0)
        can.append("Phoenix6/TalonFX-0/SupplyCurrent", "double", t, 30.0 if on else 0.0)
        can.append("Phoenix6/TalonFX-0/StatorCurrent", "double", t, 45.0 if on else 0.0)
        for cid in (4, 0, 6, 2):
            can.append(f"Phoenix6/TalonFX-{cid}/SupplyVoltage", "double", t, 11.0 if on else 12.6)
    rio.close()
    can.close()

    log_id = process_log(sess, cfg, db)
    row = db.log_row(log_id)
    assert (row["event"], row["match_type"], row["match_number"]) == ("MTTD", "qualification", 12)
    assert row["started_at"] == "2026-11-07T18:07:16+00:00"
    assert row["enabled_s"] == pytest.approx(19.8, abs=0.05)
    assert row["enable_drops"] == 1
    assert row["start_voltage"] == pytest.approx(12.6)
    assert row["min_voltage"] == pytest.approx(11.0)
    assert row["avg_current"] == pytest.approx(10 + 30)  # sum of motor supplies, rio TalonFX-0 not mixed in

    motors = {r["motor"]: r for r in db.conn.execute("SELECT * FROM motor_stats WHERE log_id=?", (log_id,))}
    assert motors["drive_fr"]["avg_stator"] == pytest.approx(45.0)        # canivore/TalonFX-0
    assert motors["shooter_top_left"]["stator_supply_ratio"] == pytest.approx(2.0)
    assert not (cfg.local_log_dir / ".converted").exists() or not any((cfg.local_log_dir / ".converted").iterdir())
    assert process_log(sess, cfg, db) is None  # dedupe

    assert count_enable_drops([(0, 20), (23, 30)]) == 0  # normal auto -> teleop gap
    assert count_enable_drops([(0, 20), (20.2, 30)]) == 1


def test_never_enabled_session_is_skipped_once(env):
    cfg, db = env
    p = cfg.local_log_dir / "bench.wpilog"
    w = WpilogWriter(p)
    w.append("DS:enabled", "boolean", 0.0, False)
    w.append("/Batt/V", "double", 0.0, 12.5)
    w.append("/Batt/V", "double", 60.0, 12.5)
    w.close()
    assert process_log(p, cfg, db) is None
    assert db.conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 0
    assert db.is_ingested(__import__("robotlogs.ingest", fromlist=["x"]).sha256_of(p))
