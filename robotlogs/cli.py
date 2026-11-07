from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from .alerts import check_anomalies
from .config import load_config
from .db import Database
from .ingest import pending_local_files, process_log
from .parse import list_signals
from .pull import pull_new_logs

log = logging.getLogger("robotlogs")


def _ingest_and_alert(paths, cfg, db) -> None:
    for path in paths:
        try:
            log_id = process_log(path, cfg, db)
        except Exception:
            log.exception("failed to process %s", path.name)
            continue
        if log_id is not None:
            for msg in check_anomalies(db, log_id, cfg):
                log.warning("⚠ %s: %s", path.name, msg)


def cmd_watch(args) -> None:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    log.info("watching for robot at %s", ", ".join(cfg.robot_hosts))
    while True:
        try:
            pull_new_logs(cfg, db)
        except Exception:
            log.exception("pull failed; will retry")
        # Always (re)process the local queue, so anything pulled earlier, or copied in by hand
        # from a USB stick, gets ingested even when the robot isn't connected.
        _ingest_and_alert(pending_local_files(cfg), cfg, db)
        if args.once:
            return
        time.sleep(cfg.poll_interval_s)


def cmd_ingest(args) -> None:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    _ingest_and_alert([Path(p) for p in args.files], cfg, db)


def cmd_signals(args) -> None:
    for name, typ, count in list_signals(Path(args.file)):
        print(f"{count:>8}  {typ:<12} {name}")


def cmd_alerts(args) -> None:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    for log_id in reversed(db.recent_log_ids(args.last)):
        row = db.log_row(log_id)
        msgs = check_anomalies(db, log_id, cfg)
        label = f"{row['event'] or '?'} {row['match_type'] or ''} {row['match_number'] or ''}".strip()
        print(f"{row['started_at']}  {label}  ({row['file_name']})")
        for m in msgs or ["ok"]:
            print(f"    {m}")


def main() -> None:
    p = argparse.ArgumentParser(prog="robotlogs")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("watch", help="poll for the robot, pull new logs, ingest them")
    s.add_argument("--once", action="store_true", help="run one cycle and exit")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("ingest", help="ingest local .wpilog/.hoot files")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("signals", help="list the signals in a .wpilog (to build signal_map)")
    s.add_argument("file")
    s.set_defaults(func=cmd_signals)

    s = sub.add_parser("alerts", help="re-run anomaly checks on recent matches")
    s.add_argument("--last", type=int, default=10)
    s.set_defaults(func=cmd_alerts)

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
