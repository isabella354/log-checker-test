"""Streamlit dashboard.

    streamlit run dashboard/app.py                     # uses config.yaml's db_path
    streamlit run dashboard/app.py -- --db other.db    # or point at a specific database
"""
import argparse
import sqlite3
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from robotlogs.config import load_config
from robotlogs.trends import LOG_TRENDS, MOTOR_TRENDS, compute_trend

ROLL = 5  # matches in the rolling average line

parser = argparse.ArgumentParser()
parser.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
parser.add_argument("--db")
args, _ = parser.parse_known_args()
cfg = load_config(args.config)
db_path = args.db or cfg.db_path
TH = cfg.thresholds

st.set_page_config(page_title="Robot Trends", layout="wide")

# ---------- colors: categorical slots 1-3, stepped separately for light and dark ----------
try:
    DARK = st.context.theme.type == "dark"
except Exception:
    DARK = False
SERIES = ["#3987e5", "#d95926", "#199e70"] if DARK else ["#2a78d6", "#eb6834", "#1baf7a"]
MUTED = "#8a8984" if DARK else "#7a7974"


# ---------- data ----------
@st.cache_data(ttl=15)
def load(path: str):
    conn = sqlite3.connect(path)
    logs = pd.read_sql("SELECT * FROM logs ORDER BY started_at, id", conn, parse_dates=["started_at"])
    motors = pd.read_sql(
        "SELECT ms.*, l.started_at, l.event, l.match_type, l.match_number "
        "FROM motor_stats ms JOIN logs l ON l.id = ms.log_id ORDER BY l.started_at, l.id",
        conn, parse_dates=["started_at"],
    )
    return logs, motors


def match_label(r) -> str:
    abbrev = {"qualification": "Q", "elimination": "E", "practice": "P"}.get(r.match_type or "", "")
    if r.event and r.match_number:
        return f"{r.event} {abbrev}{int(r.match_number)}"
    return f"{r.started_at:%b %d %H:%M}" if pd.notna(r.started_at) else r.file_name


if not Path(db_path).exists():
    st.title("Robot trends")
    st.info(f"No database at `{db_path}` yet. Run `robotlogs watch` or `robotlogs ingest <file>` first.")
    st.stop()

logs, motors = load(str(db_path))
st.title("Robot trends")
if logs.empty:
    st.info("No matches ingested yet. Run `robotlogs watch` or `robotlogs ingest <file>`.")
    st.stop()

# ---------- filters (one row, above everything) ----------
f1, f2, _ = st.columns([2, 2, 3])
events = f1.multiselect("Event", sorted(logs["event"].dropna().unique()))
types = f2.multiselect("Match type", sorted(logs["match_type"].dropna().unique()))
if events:
    logs, motors = logs[logs.event.isin(events)], motors[motors.event.isin(events)]
if types:
    logs, motors = logs[logs.match_type.isin(types)], motors[motors.match_type.isin(types)]
if logs.empty:
    st.warning("No matches match these filters.")
    st.stop()

logs = logs.reset_index(drop=True)
logs["n"] = logs.index + 1  # play order within the current filter
logs["match"] = logs.apply(match_label, axis=1)
motors = motors.merge(logs[["id", "n", "match"]], left_on="log_id", right_on="id", suffixes=("", "_l"))

st.caption(f"{len(logs)} matches. In every chart, each dot is one match and the line is the "
           f"{ROLL}-match rolling average. Hover for exact values.")


# ---------- chart helpers ----------
def trend_chart(df: pd.DataFrame, series: dict[str, str], title: str, y_title: str, fmt: str = ".2f",
                rules: list[tuple[float, str]] = (), height: int = 260, zero: bool = False):
    """Dots per match + rolling-average line per series, on one shared y axis.
    `title` describes the chart; `y_title` is the short axis label (usually just the unit)."""
    cols = [c for c in series.values() if c in df and df[c].notna().any()]
    if not cols:
        st.caption(f"*{title}: no data yet (check signal names in config.yaml).*")
        return
    names = {c: label for label, c in series.items() if c in cols}
    long = (df[["n", "match", *cols]].melt(id_vars=["n", "match"], var_name="col", value_name="value")
            .dropna(subset=["value"]))
    long["series"] = long["col"].map(names)
    long["rolling"] = long.groupby("series")["value"].transform(lambda s: s.rolling(ROLL, min_periods=1).mean())
    labels = list(names.values())

    color = alt.Color("series:N", title=None,
                      scale=alt.Scale(domain=labels, range=SERIES[:len(labels)]),
                      legend=alt.Legend(orient="top") if len(labels) > 1 else None)
    x = alt.X("n:Q", title="Match (in order played)", axis=alt.Axis(tickMinStep=1, format="d"),
              scale=alt.Scale(domain=[1, max(2, int(df["n"].max()))], nice=False))
    y_scale = alt.Scale(zero=zero)
    y_axis = alt.Axis(format=",~r")  # 1,500 / 0.025 / 3.2 — trims float noise like 3.2000000001
    base = alt.Chart(long)

    dots = base.mark_circle(size=64, opacity=0.4).encode(x=x, y=alt.Y("value:Q", title=y_title, scale=y_scale, axis=y_axis), color=color)
    line = base.mark_line(strokeWidth=2).encode(x=x, y=alt.Y("rolling:Q", scale=y_scale), color=color)

    hover = alt.selection_point(fields=["n"], nearest=True, on="pointerover", empty=False, clear="pointerout")
    wide = base.transform_pivot("series", value="value", groupby=["n", "match"])
    # invisible wide rule per match: catches the pointer and carries the tooltip for every series
    catcher = wide.mark_rule(opacity=0, strokeWidth=14).encode(
        x=x,
        tooltip=[alt.Tooltip("match:N", title="Match")] + [alt.Tooltip(f"{l}:Q", format=fmt) for l in labels],
    ).add_params(hover)
    cursor = wide.mark_rule(color=MUTED, strokeWidth=1).encode(x=x).transform_filter(hover)

    layers = [dots, line, catcher, cursor]
    for value, text in rules:
        r = pd.DataFrame({"y": [value], "t": [text]})
        layers.append(alt.Chart(r).mark_rule(color=MUTED, strokeDash=[4, 4], strokeWidth=1).encode(y="y:Q"))
        layers.append(alt.Chart(r).mark_text(align="left", dx=4, dy=-6, color=MUTED, fontSize=11)
                      .encode(y="y:Q", text="t:N", x=alt.value(0)))

    st.altair_chart(alt.layer(*layers).properties(height=height, title=title), width="stretch")
    with st.expander("Data"):
        table = df[["match", *cols]].rename(columns=names).set_index("match")
        st.dataframe(table, width="stretch")


def bar_chart(df: pd.DataFrame, cat: str, val: str, title: str, unit: str, fmt: str = ".3f",
              weakest: str = "high"):
    """Horizontal bars, one color, weakest at the top: comparing magnitude across categories."""
    d = df[[cat, val]].dropna()
    if d.empty:
        return
    chart = alt.Chart(d).mark_bar(cornerRadiusEnd=4, color=SERIES[0], height={"band": 0.7}).encode(
        y=alt.Y(f"{cat}:N", sort="-x" if weakest == "high" else "x", title=None),
        x=alt.X(f"{val}:Q", title=unit, axis=alt.Axis(format=",~r")),
        tooltip=[alt.Tooltip(f"{cat}:N", title="Battery"), alt.Tooltip(f"{val}:Q", format=fmt, title=title)],
    ).properties(height={"step": 30}, title=title)
    st.altair_chart(chart, width="stretch")


def fmt_num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.3g}"


def trend_frame(trends) -> pd.DataFrame:
    icon = {"worse": "▲ worse", "better": "▼ better", "steady": "– steady"}
    return pd.DataFrame([{
        "Metric": t.name, "Unit": t.unit, "Matches": t.matches,
        f"First {min(ROLL, t.matches // 2)} avg": round(t.early, 3),
        f"Last {min(ROLL, t.matches // 2)} avg": round(t.recent, 3),
        "Change": f"{t.change_pct:+.1f}%" if t.change_pct is not None else "n/a",
        "Per match": round(t.per_match, 4),
        "Trend": icon[t.verdict],
    } for t in trends])


tab_overview, tab_power, tab_volt, tab_motor, tab_batt, tab_match = st.tabs(
    ["Overview", "Power", "Battery voltage", "Motors", "Batteries", "Match detail"])

# ---------- Overview ----------
with tab_overview:
    last = logs.iloc[-1]
    prev = logs.iloc[:-1]
    st.subheader(f"Latest match: {last.match}")
    st.caption("Change is compared with the average of all earlier matches in this view.")
    tiles = [
        ("Energy used", "energy_wh", "Wh", "inverse"),
        ("Average power", "avg_power_w", "W", "inverse"),
        ("Peak current", "peak_current", "A", "inverse"),
        ("Starting voltage", "start_voltage", "V", "normal"),
        ("Minimum voltage", "min_voltage", "V", "normal"),
        ("Sag per amp", "sag_ohms", "Ω", "inverse"),
    ]
    for col, (label, key, unit, good) in zip(st.columns(len(tiles)), tiles):
        v = last.get(key)
        if pd.isna(v):
            col.metric(label, "—")
            continue
        avg = prev[key].mean() if len(prev) else float("nan")
        delta = None if pd.isna(avg) else f"{'+' if v >= avg else '−'}{fmt_num(abs(v - avg))} {unit}"
        col.metric(label, f"{fmt_num(v)} {unit}", delta, delta_color=good)

    st.subheader("Is it getting better or worse?")
    st.caption("Compares the first matches with the most recent ones. ▲ worse / ▼ better means a change "
               "of 5% or more in the bad / good direction; – steady means less than 5%.")
    season = [t for col, label, unit, worse in LOG_TRENDS
              if (t := compute_trend(label, unit, logs[col].tolist(), worse))]
    if season:
        st.dataframe(trend_frame(season), hide_index=True, width="stretch")
    else:
        st.caption("*Needs at least 4 matches with data.*")

    st.subheader("By event")
    by_event = (logs.assign(event=logs.event.fillna("(unknown)"))
                .groupby("event", sort=False)
                .agg(matches=("id", "count"), first=("started_at", "min"),
                     total_energy_wh=("energy_wh", "sum"), avg_energy_wh=("energy_wh", "mean"),
                     avg_power_w=("avg_power_w", "mean"), avg_min_voltage=("min_voltage", "mean"),
                     lowest_voltage=("min_voltage", "min"), brownouts=("brownouts", "sum"))
                .sort_values("first").drop(columns="first").round(2))
    st.dataframe(by_event, width="stretch")

# ---------- Power ----------
with tab_power:
    st.caption("Power comes from battery voltage × total current while the robot is enabled. "
               "If total current isn't logged, it's the sum of the tracked motors' supply currents.")
    trend_chart(logs, {"Energy": "energy_wh"}, "Energy used per match", "Wh", fmt=".2f", zero=True)
    c1, c2 = st.columns(2)
    with c1:
        trend_chart(logs, {"Average": "avg_power_w", "Peak": "peak_power_w"}, "Power", "W", fmt=".0f", zero=True)
    with c2:
        trend_chart(logs, {"Average": "avg_current", "Peak": "peak_current"}, "Total current", "A", fmt=".1f", zero=True)

# ---------- Battery voltage ----------
with tab_volt:
    trend_chart(
        logs, {"Starting": "start_voltage", "Average": "avg_voltage", "Minimum": "min_voltage"},
        "Battery voltage", "V", fmt=".2f",
        rules=[(TH.low_voltage, f"low {TH.low_voltage:g} V"), (TH.critical_voltage, f"critical {TH.critical_voltage:g} V")],
        height=320,
    )
    c1, c2 = st.columns(2)
    with c1:
        trend_chart(logs, {"Sag": "sag_ohms"}, "Voltage sag per amp (higher = weaker battery or wiring)", "Ω", fmt=".4f")
    with c2:
        trend_chart(logs, {"Below low": "time_below_low_v", "Below critical": "time_below_crit_v"},
                    "Time below voltage thresholds", "seconds", fmt=".1f", zero=True)
    b = logs[["n", "match", "brownouts"]].dropna()
    if not b.empty:
        st.altair_chart(
            alt.Chart(b).mark_bar(color=SERIES[0], cornerRadiusEnd=4).encode(
                x=alt.X("n:O", title="Match (in order played)", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("brownouts:Q", title="Brownouts", axis=alt.Axis(tickMinStep=1, format="d")),
                tooltip=["match:N", "brownouts:Q"],
            ).properties(height=180, title="Brownouts per match"),
            width="stretch",
        )

# ---------- Motors ----------
with tab_motor:
    if motors.empty:
        st.info("No motor stats yet. Add motors under `signal_map.motors` in config.yaml.")
    else:
        st.caption("**Supply current** is what the motor draws from the battery. **Stator current** is the "
                   "current in the motor windings, which is proportional to torque. Stator much higher than "
                   "supply means lots of torque at low speed (pushing, holding, stalling). If stator current "
                   "for the same job creeps up over weeks, suspect friction or a failing bearing.")
        mtrends = [t for m, g in motors.groupby("motor") for col, label, unit, worse in MOTOR_TRENDS
                   if (t := compute_trend(f"{m}: {label}", unit, g[col].tolist(), worse))]
        if mtrends:
            with st.expander("Trend table (all motors)", expanded=True):
                st.dataframe(trend_frame(mtrends), hide_index=True, width="stretch")

        all_motors = sorted(motors.motor.unique())
        m1, m2 = st.columns([3, 1])
        pick = m1.multiselect("Motors", all_motors, default=all_motors)
        kind = m2.radio("Current", ["Average", "Peak"], horizontal=True)
        pre = "avg" if kind == "Average" else "peak"
        for m in pick:
            g = motors[motors.motor == m].sort_values("n")
            st.markdown(f"#### {m}")
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1:
                trend_chart(g, {"Supply": f"{pre}_supply", "Stator": f"{pre}_stator"},
                            f"{kind} current: supply vs stator", "A", fmt=".1f", zero=True)
            with c2:
                trend_chart(g, {"Ratio": "stator_supply_ratio"}, "Stator ÷ supply", "ratio", fmt=".2f", zero=True)
            with c3:
                trend_chart(g, {"Peak temp": "peak_temp"}, "Peak temperature", "°C", fmt=".0f",
                            rules=[(TH.hot_motor_c, f"hot {TH.hot_motor_c:g} °C")])

# ---------- Batteries ----------
with tab_batt:
    if logs["battery_id"].notna().any():
        bl = logs.dropna(subset=["battery_id"])
        per = (bl.groupby("battery_id")
               .agg(matches=("id", "count"), avg_start_v=("start_voltage", "mean"),
                    avg_min_v=("min_voltage", "mean"), avg_sag_ohms=("sag_ohms", "mean"),
                    brownouts=("brownouts", "sum"), last_used=("started_at", "max"))
               .sort_values("avg_sag_ohms", ascending=False).reset_index())
        c1, c2 = st.columns(2)
        with c1:
            bar_chart(per, "battery_id", "avg_sag_ohms", "Avg sag per amp (higher = weaker)", "Ω", fmt=".4f")
        with c2:
            bar_chart(per, "battery_id", "avg_min_v", "Avg minimum voltage (lower = weaker)", "V", fmt=".2f",
                      weakest="low")
        st.dataframe(per.round({c: 4 for c in per.select_dtypes("number").columns}),
                     hide_index=True, width="stretch")
        st.caption("Consider retiring batteries that are consistently at the top of the sag chart.")
    else:
        st.info("No battery IDs logged. Publish a battery name from robot code (e.g. a dashboard dropdown) "
                "and set `battery_id` in config.yaml to enable per-battery tracking.")

# ---------- Match detail ----------
with tab_match:
    choice = st.selectbox("Match", logs.index[::-1], format_func=lambda i: logs.loc[i, "match"])
    row = logs.loc[choice]
    st.dataframe(row.drop(labels=["n"]).to_frame("value").astype(str), width="stretch")
    st.dataframe(motors[motors.log_id == row["id"]]
                 [["motor", "avg_supply", "peak_supply", "avg_stator", "peak_stator",
                   "stator_supply_ratio", "peak_temp", "time_hot"]].round(2),
                 hide_index=True, width="stretch")
