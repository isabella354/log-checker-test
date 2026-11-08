"""Per-match metrics.

Logged signals are irregular (NetworkTables entries are only logged on change), so every
signal is resampled with a zero-order hold onto a fixed grid covering only the time the
robot was enabled. On that grid, "time below X" is just count * dt, energy is sum(V*I) * dt,
and so on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .config import Config
    from .parse import Signals


@dataclass
class Series:
    t: np.ndarray  # seconds (robot FPGA time)
    v: np.ndarray

    def sample(self, grid: np.ndarray) -> np.ndarray:
        """Zero-order hold: value of the most recent sample at each grid time (NaN before the first)."""
        idx = np.searchsorted(self.t, grid, side="right") - 1
        out = np.full(grid.shape, np.nan)
        ok = idx >= 0
        out[ok] = self.v[idx[ok]]
        return out


def enabled_intervals(enabled: Series | None, start: float, end: float) -> list[tuple[float, float]]:
    """Time spans where the robot was enabled. With no enabled signal, use the whole log."""
    if enabled is None or len(enabled.t) == 0:
        return [(start, end)] if end > start else []
    out, on_since = [], None
    for t, v in zip(enabled.t, enabled.v):
        if v > 0.5 and on_since is None:
            on_since = t
        elif v <= 0.5 and on_since is not None:
            out.append((on_since, t))
            on_since = None
    if on_since is not None and end > on_since:
        out.append((on_since, end))
    return out


def make_grid(intervals: list[tuple[float, float]], dt: float) -> np.ndarray:
    if not intervals:
        return np.empty(0)
    return np.concatenate([np.arange(a, b, dt) for a, b in intervals])


def _nan_stat(fn, arr: np.ndarray | None) -> float | None:
    if arr is None or arr.size == 0 or np.all(np.isnan(arr)):
        return None
    return float(fn(arr))


def _time_where(cond: np.ndarray, dt: float) -> float:
    return float(np.count_nonzero(cond)) * dt


def _rising_edges_in(series: Series | None, intervals: list[tuple[float, float]]) -> int | None:
    if series is None:
        return None
    on = series.v > 0.5
    rising_t = series.t[1:][on[1:] & ~on[:-1]]
    if on.size and on[0]:
        rising_t = np.concatenate([series.t[:1], rising_t])
    return int(sum(np.count_nonzero((rising_t >= a) & (rising_t < b)) for a, b in intervals))


@dataclass
class Window:
    grid: np.ndarray
    dt: float
    intervals: list[tuple[float, float]]

    def get(self, signals: Signals, name: str | None) -> np.ndarray | None:
        s = signals.numeric.get(name) if name else None
        return s.sample(self.grid) if s is not None else None


def match_window(signals: Signals, cfg: Config) -> Window:
    sm = cfg.signal_map
    enabled = signals.numeric.get(sm.enabled) if sm.enabled else None
    intervals = enabled_intervals(enabled, signals.start_s, signals.end_s)
    dt = 1.0 / cfg.sample_rate_hz
    return Window(make_grid(intervals, dt), dt, intervals)


def compute_match_summary(signals: Signals, cfg: Config, win: Window) -> dict:
    sm, th = cfg.signal_map, cfg.thresholds
    v = win.get(signals, sm.battery_voltage)
    i = win.get(signals, sm.total_current)
    if i is None:  # fall back to summing motor supply currents
        parts = [win.get(signals, m.supply) for m in sm.motors.values()]
        parts = [np.abs(p) for p in parts if p is not None]
        i = np.nansum(parts, axis=0) if parts else None

    # Voltage just before the first enable ≈ resting voltage at the start of the match.
    start_v = None
    vs = signals.numeric.get(sm.battery_voltage) if sm.battery_voltage else None
    if vs is not None and win.intervals:
        just_before = win.intervals[0][0] - 1e-3  # last sample strictly before enable, not under load
        start_v = _nan_stat(lambda a: a[0], vs.sample(np.array([just_before])))

    energy_wh = sag = avg_power = peak_power = None
    if v is not None and i is not None:
        both = ~np.isnan(v) & ~np.isnan(i)
        power = v[both] * i[both]  # watts, at each grid step
        if power.size:
            energy_wh = float(np.sum(power) * win.dt / 3600.0)
            avg_power = float(np.mean(power))
            peak_power = float(np.max(power))
        if np.count_nonzero(both) > 10 and np.ptp(i[both]) > 1.0:
            slope = np.polyfit(i[both], v[both], 1)[0]
            sag = float(-slope)  # volts lost per amp ≈ battery + wiring resistance (ohms)

    return {
        "enabled_s": float(win.grid.size * win.dt),
        "start_voltage": start_v,
        "min_voltage": _nan_stat(np.nanmin, v),
        "avg_voltage": _nan_stat(np.nanmean, v),
        "time_below_low_v": _time_where(v < th.low_voltage, win.dt) if v is not None else None,
        "time_below_crit_v": _time_where(v < th.critical_voltage, win.dt) if v is not None else None,
        "brownouts": _rising_edges_in(signals.numeric.get(sm.brownout) if sm.brownout else None, win.intervals),
        "peak_current": _nan_stat(np.nanmax, i),
        "avg_current": _nan_stat(np.nanmean, i),
        "energy_wh": energy_wh,
        "avg_power_w": avg_power,
        "peak_power_w": peak_power,
        "sag_ohms": sag,
    }


def compute_motor_stats(signals: Signals, cfg: Config, win: Window) -> list[dict]:
    rows = []
    for name, m in cfg.signal_map.motors.items():
        supply = win.get(signals, m.supply)
        stator = win.get(signals, m.stator)
        temp = win.get(signals, m.temp)
        if supply is None and stator is None and temp is None:
            continue  # motor not present in this log (e.g. it's in the .hoot, not the .wpilog)
        supply = np.abs(supply) if supply is not None else None
        stator = np.abs(stator) if stator is not None else None

        avg_supply = _nan_stat(np.nanmean, supply)
        avg_stator = _nan_stat(np.nanmean, stator)
        ratio = None
        if avg_supply is not None and avg_stator is not None:
            ratio = avg_stator / max(avg_supply, 0.01)

        rows.append({
            "motor": name,
            "peak_supply": _nan_stat(np.nanmax, supply),
            "avg_supply": avg_supply,
            "peak_stator": _nan_stat(np.nanmax, stator),
            "avg_stator": avg_stator,
            "stator_supply_ratio": ratio,
            "peak_temp": _nan_stat(np.nanmax, temp),
            "time_hot": _time_where(temp > cfg.thresholds.hot_motor_c, win.dt) if temp is not None else None,
        })
    return rows
