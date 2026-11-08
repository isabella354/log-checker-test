"""Season trends: is each metric getting better, worse, or holding steady?

Compares the average of the first few matches with the average of the most recent few,
and fits a straight line to get the change per match. Used by `robotlogs trends` and the
dashboard, so both always agree.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .db import Database

# (column, label, unit, higher_is_worse)
LOG_TRENDS = [
    ("energy_wh", "Energy per match", "Wh", True),
    ("avg_power_w", "Average power", "W", True),
    ("peak_power_w", "Peak power", "W", True),
    ("avg_current", "Average current", "A", True),
    ("peak_current", "Peak current", "A", True),
    ("start_voltage", "Starting voltage", "V", False),
    ("avg_voltage", "Average voltage", "V", False),
    ("min_voltage", "Minimum voltage", "V", False),
    ("sag_ohms", "Voltage sag per amp", "Ω", True),
    ("time_below_low_v", "Time below low-voltage", "s", True),
    ("brownouts", "Brownouts", "", True),
]
MOTOR_TRENDS = [
    ("avg_supply", "avg supply current", "A", True),
    ("avg_stator", "avg stator current", "A", True),
    ("peak_stator", "peak stator current", "A", True),
    ("stator_supply_ratio", "stator ÷ supply", "×", True),
    ("peak_temp", "peak temperature", "°C", True),
]

WINDOW = 5         # matches averaged at each end
STEADY_TOL = 0.05  # changes under 5% count as steady


@dataclass
class Trend:
    name: str
    unit: str
    matches: int
    early: float     # mean of the first WINDOW matches
    recent: float    # mean of the last WINDOW matches
    change_pct: float | None
    per_match: float  # slope of a straight-line fit, in units per match
    verdict: str     # "worse" | "better" | "steady"


def compute_trend(name: str, unit: str, values: list[float | None], higher_is_worse: bool) -> Trend | None:
    """values must be in the order the matches were played. Needs at least 4 matches."""
    vals = np.array([v for v in values if v is not None and not np.isnan(v)], dtype=float)
    if vals.size < 4:
        return None
    k = min(WINDOW, vals.size // 2)
    early, recent = float(vals[:k].mean()), float(vals[-k:].mean())
    slope = float(np.polyfit(np.arange(vals.size), vals, 1)[0])

    if early != 0:
        change = (recent - early) / abs(early)
        moved = abs(change) >= STEADY_TOL
    else:
        change = None
        moved = recent != early
    if not moved:
        verdict = "steady"
    else:
        verdict = "worse" if (recent > early) == higher_is_worse else "better"
    return Trend(name, unit, int(vals.size), early, recent,
                 None if change is None else change * 100, slope, verdict)


def _filters(event: str | None, match_type: str | None, alias: str = "") -> tuple[str, list]:
    p = f"{alias}." if alias else ""
    where, args = [], []
    if event:
        where.append(f"{p}event = ?")
        args.append(event)
    if match_type:
        where.append(f"{p}match_type = ?")
        args.append(match_type)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def season_trends(db: Database, event: str | None = None, match_type: str | None = None) -> list[Trend]:
    where, args = _filters(event, match_type)
    rows = db.conn.execute(f"SELECT * FROM logs{where} ORDER BY started_at, id", args).fetchall()
    out = []
    for col, label, unit, worse in LOG_TRENDS:
        t = compute_trend(label, unit, [r[col] for r in rows], worse)
        if t:
            out.append(t)
    return out


def motor_trends(db: Database, event: str | None = None, match_type: str | None = None) -> list[Trend]:
    where, args = _filters(event, match_type, alias="l")
    rows = db.conn.execute(
        f"""SELECT ms.* FROM motor_stats ms JOIN logs l ON l.id = ms.log_id{where}
            ORDER BY l.started_at, l.id""", args,
    ).fetchall()
    by_motor: dict[str, list] = {}
    for r in rows:
        by_motor.setdefault(r["motor"], []).append(r)
    out = []
    for motor, mrows in sorted(by_motor.items()):
        for col, label, unit, worse in MOTOR_TRENDS:
            t = compute_trend(f"{motor}: {label}", unit, [r[col] for r in mrows], worse)
            if t:
                out.append(t)
    return out


def format_table(trends: list[Trend]) -> str:
    icon = {"worse": "▲ worse", "better": "▼ better", "steady": "– steady"}
    lines = [f"{'Metric':<34} {'Matches':>7} {'Early':>9} {'Recent':>9} {'Change':>8} {'Per match':>10}  Trend"]
    for t in trends:
        chg = f"{t.change_pct:+.1f}%" if t.change_pct is not None else "n/a"
        lines.append(
            f"{(t.name + (f' ({t.unit})' if t.unit else '')):<34} {t.matches:>7} {t.early:>9.4g} "
            f"{t.recent:>9.4g} {chg:>8} {t.per_match:>+10.3g}  {icon[t.verdict]}"
        )
    return "\n".join(lines)
