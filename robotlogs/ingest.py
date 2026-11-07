"""Turn one log file into a row in the database."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Database
from .metrics import compute_match_summary, compute_motor_stats, match_window
from .parse import Signals, convert_hoot, read_wpilog

log = logging.getLogger(__name__)

MATCH_TYPES = {0: "none", 1: "practice", 2: "qualification", 3: "elimination"}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_match_info(signals: Signals, cfg: Config, path: Path) -> dict:
    sm = cfg.signal_map
    mt = signals.last_number(sm.match_type)
    mn = signals.last_number(sm.match_number)
    return {
        "event": signals.last_string(sm.event_name) or None,
        "match_type": MATCH_TYPES.get(int(mt), str(int(mt))) if mt is not None else None,
        "match_number": int(mn) if mn else None,
        "battery_id": signals.last_string(sm.battery_id) or None,
        "started_at": _wall_clock_start(signals, cfg, path),
    }


def _wall_clock_start(signals: Signals, cfg: Config, path: Path) -> str:
    """DataLogManager logs `systemTime` (µs since epoch) once the clock is valid.
    Back it out to the log's first timestamp; fall back to the file's mtime."""
    st = signals.numeric.get(cfg.signal_map.system_time) if cfg.signal_map.system_time else None
    if st is not None and len(st.t):
        epoch_s = st.v[0] / 1e6 - (st.t[0] - signals.start_s)
    else:
        epoch_s = path.stat().st_mtime
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="seconds")


def process_log(path: Path, cfg: Config, db: Database) -> int | None:
    """Returns the new log id, or None if this exact file was already ingested."""
    digest = sha256_of(path)
    if db.is_ingested(digest):
        return None

    wpilog = convert_hoot(path, cfg.owlet_cmd) if path.suffix == ".hoot" else path
    signals = read_wpilog(wpilog, wanted=cfg.signal_map.all_names())

    missing = sorted(n for n in cfg.signal_map.all_names()
                     if n not in signals.numeric and n not in signals.strings)
    if missing:
        log.debug("%s: signals not found: %s", path.name, ", ".join(missing))

    win = match_window(signals, cfg)
    info = extract_match_info(signals, cfg, path)
    summary = compute_match_summary(signals, cfg, win)
    motors = compute_motor_stats(signals, cfg, win)
    log_id = db.insert_log(path.name, digest, info, summary, motors)
    log.info("ingested %s: %s %s%s, %.0fs enabled, %d motors",
             path.name, info["event"] or "?", info["match_type"] or "",
             f" #{info['match_number']}" if info["match_number"] else "",
             summary["enabled_s"], len(motors))
    return log_id


def pending_local_files(cfg: Config) -> list[Path]:
    """Everything in local_log_dir (not subfolders), oldest first. The sha256 check in
    process_log skips ones already ingested, so this doubles as the offline queue."""
    if not cfg.local_log_dir.exists():
        return []
    files = [p for p in cfg.local_log_dir.iterdir()
             if p.is_file() and p.suffix in (".wpilog", ".hoot")]
    return sorted(files, key=lambda p: p.stat().st_mtime)
