"""Streamlit dashboard: `streamlit run dashboard/app.py -- --db robot_trends.db`"""
import argparse
import sqlite3

import pandas as pd
import streamlit as st

parser = argparse.ArgumentParser()
parser.add_argument("--db", default="robot_trends.db")
args, _ = parser.parse_known_args()

st.set_page_config(page_title="Robot Trends", layout="wide")
st.title("Robot trends")

conn = sqlite3.connect(args.db)
logs = pd.read_sql("SELECT * FROM logs ORDER BY started_at", conn, parse_dates=["started_at"])
motors = pd.read_sql(
    "SELECT ms.*, l.started_at, l.event, l.match_type, l.match_number "
    "FROM motor_stats ms JOIN logs l ON l.id = ms.log_id ORDER BY l.started_at",
    conn, parse_dates=["started_at"],
)
if logs.empty:
    st.info("No matches ingested yet. Run `robotlogs watch` or `robotlogs ingest <file>`.")
    st.stop()

with st.sidebar:
    events = st.multiselect("Event", sorted(logs["event"].dropna().unique()))
    types = st.multiselect("Match type", sorted(logs["match_type"].dropna().unique()))
    if events:
        logs, motors = logs[logs.event.isin(events)], motors[motors.event.isin(events)]
    if types:
        logs, motors = logs[logs.match_type.isin(types)], motors[motors.match_type.isin(types)]

tab_season, tab_battery, tab_motor, tab_match = st.tabs(["Season", "Batteries", "Motors", "Match"])

with tab_season:
    c1, c2 = st.columns(2)
    c1.subheader("Minimum voltage")
    c1.line_chart(logs, x="started_at", y="min_voltage")
    c2.subheader("Energy used (Wh)")
    c2.line_chart(logs, x="started_at", y="energy_wh")
    c1.subheader("Time below low-voltage threshold (s)")
    c1.bar_chart(logs, x="started_at", y="time_below_low_v")
    c2.subheader("Brownouts")
    c2.bar_chart(logs, x="started_at", y="brownouts")

with tab_battery:
    if logs["battery_id"].notna().any():
        per = (logs.dropna(subset=["battery_id"]).groupby("battery_id")
               .agg(matches=("id", "count"), start_v=("start_voltage", "mean"),
                    min_v=("min_voltage", "mean"), sag_ohms=("sag_ohms", "mean"),
                    brownouts=("brownouts", "sum"), last_used=("started_at", "max"))
               .sort_values("sag_ohms", ascending=False))
        st.dataframe(per, use_container_width=True)
        st.subheader("Sag per amp over time (higher = weaker battery)")
        st.line_chart(logs.dropna(subset=["battery_id"]), x="started_at", y="sag_ohms", color="battery_id")
    else:
        st.info("No battery IDs logged. Add a `battery_id` signal to config.yaml to enable per-battery tracking.")

with tab_motor:
    if motors.empty:
        st.info("No motor stats yet.")
    else:
        pick = st.multiselect("Motors", sorted(motors.motor.unique()), default=sorted(motors.motor.unique())[:4])
        m = motors[motors.motor.isin(pick)]
        metric = st.selectbox("Metric", ["avg_stator", "peak_stator", "avg_supply", "peak_supply",
                                         "stator_supply_ratio", "peak_temp", "time_hot"])
        st.line_chart(m, x="started_at", y=metric, color="motor")

with tab_match:
    labels = logs.apply(lambda r: f"{r.started_at:%Y-%m-%d %H:%M}  {r.event or ''} "
                                  f"{r.match_type or ''} {r.match_number or ''}  ({r.file_name})", axis=1)
    choice = st.selectbox("Match", labels.index[::-1], format_func=lambda i: labels[i])
    st.dataframe(logs.loc[[choice]].T, use_container_width=True)
    st.dataframe(motors[motors.log_id == logs.loc[choice, "id"]].drop(columns=["log_id"]),
                 use_container_width=True)
