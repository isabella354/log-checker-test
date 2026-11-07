"""Flag matches that are out of line with recent history."""
from __future__ import annotations

import numpy as np

from .config import Config
from .db import Database

# metric -> human label. Direction doesn't matter for a z-score, both tails are interesting.
LOG_CHECKS = {
    "energy_wh": "energy used",
    "min_voltage": "minimum voltage",
    "sag_ohms": "voltage sag per amp",
    "peak_current": "peak current",
}
MOTOR_CHECKS = {
    "avg_stator": "avg stator current",
    "peak_temp": "peak temperature",
}


def _z(value: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    sd = float(np.std(history, ddof=1))
    return None if sd == 0 else (value - float(np.mean(history))) / sd


def check_anomalies(db: Database, log_id: int, cfg: Config) -> list[str]:
    a = cfg.alerts
    row = db.log_row(log_id)
    out: list[str] = []

    for metric, label in LOG_CHECKS.items():
        value = row[metric]
        hist = db.history(metric, log_id, a.history_matches)
        if value is None or len(hist) < a.min_history:
            continue
        z = _z(value, hist)
        if z is not None and abs(z) > a.z_threshold:
            out.append(f"{label} = {value:.2f} is {z:+.1f}σ from the last {len(hist)} matches")

    for motor in db.motors_in(log_id):
        for metric, label in MOTOR_CHECKS.items():
            value = db.motor_value(log_id, motor, metric)
            series = db.motor_history(motor, metric, log_id, a.history_matches + 1)
            hist = series[:-1]
            if value is None or len(hist) < a.min_history:
                continue
            z = _z(value, hist)
            if z is not None and abs(z) > a.z_threshold:
                out.append(f"{motor}: {label} = {value:.1f} is {z:+.1f}σ from its recent average")

        # Slow drift: fit a line through avg stator current over recent matches.
        series = db.motor_history(motor, "avg_stator", log_id, a.history_matches)
        if len(series) >= max(a.min_history, 8):
            slope = np.polyfit(np.arange(len(series)), series, 1)[0]
            rel = slope / max(float(np.mean(series)), 1e-6)
            if rel > a.wear_slope_per_match:
                out.append(f"{motor}: avg stator current rising {rel:.1%}/match over "
                           f"{len(series)} matches (friction or wear?)")
    return out
