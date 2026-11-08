"""Turn one log file into a row in the database."""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import Config
from .db import Database
from .metrics import compute_match_summary, compute_motor_stats, match_window
from .parse import Signals, merge, read_hoot, read_wpilog

log = logging.getLogger(__name__)

MATCH_TYPES = {0: "none", 1: "practice", 2: "qualification", 3: "elimination"}
_TYPE_LETTERS = {"Q": "qualification", "P": "practice", "E": "elimination"}
# CTRE names match folders/files "<event>_<Q|P|E><number>", e.g. mttd_Q12/mttd_Q12_rio_....hoot
_MATCH_IN_NAME = re.compile(r"(?:^|[/_])([A-Za-z0-9]+)_([QPE])(\d+)(?=_|$)")
# CTRE and AdvantageKit put the UTC start time in file names: 2026-11-07_18-07-16 / 26-03-07_18-30-12
_TIME_IN_NAME = re.compile(r"(\d{2,4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")
LOG_SUFFIXES = (".wpilog", ".hoot")
CONVERTED_DIR = ".converted"


@dataclass
class Session:
    """One robot power-on: a single .wpilog, or a CTRE folder with one .hoot per CAN bus."""
    name: str
    files: list[Path]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def session_digest(files: list[Path]) -> str:
    if len(files) == 1:
        return sha256_of(files[0])
    return hashlib.sha256("".join(sorted(sha256_of(f) for f in files)).encode()).hexdigest()


def _match_type(signals: Signals, name: str | None) -> str | None:
    n = signals.last_number(name)
    if n is not None:
        return MATCH_TYPES.get(int(n), str(int(n)))
    s = (signals.last_string(name) or "").strip().lower()  # CTRE writes "Qualification" etc.
    return s if s and s != "none" else None


def _match_number(signals: Signals, name: str | None) -> int | None:
    n = signals.last_number(name)
    if n is None:
        s = (signals.last_string(name) or "").strip()
        n = int(s) if s.isdigit() else None
    return int(n) if n else None


def extract_match_info(signals: Signals, cfg: Config, session: Session) -> dict:
    sm = cfg.signal_map
    info = {
        "event": (signals.last_string(sm.event_name) or "").strip() or None,
        "match_type": _match_type(signals, sm.match_type),
        "match_number": _match_number(signals, sm.match_number),
        "battery_id": signals.last_string(sm.battery_id) or None,
        "started_at": _wall_clock_start(signals, cfg, session),
    }
    # Fall back to the folder/file name when the log didn't capture FMS info.
    if not (info["event"] and info["match_number"]):
        for text in [session.name, *(f.name for f in session.files)]:
            if m := _MATCH_IN_NAME.search(text):
                info["event"] = info["event"] or m.group(1).upper()
                info["match_type"] = info["match_type"] or _TYPE_LETTERS[m.group(2)]
                info["match_number"] = info["match_number"] or int(m.group(3))
                break
    return info


def _wall_clock_start(signals: Signals, cfg: Config, session: Session) -> str:
    """Wall clock at the start of the log. Prefer a logged epoch-time signal, then the UTC
    timestamp in the file name, then the file's modified time."""
    st = signals.numeric.get(cfg.signal_map.system_time) if cfg.signal_map.system_time else None
    valid = st.v > 1e15 if st is not None else None  # ignore 0 / unset clock (µs before ~2001)
    if st is not None and valid.any():
        i = int(np.argmax(valid))
        epoch_s = st.v[i] / 1e6 - (st.t[i] - signals.start_s)
        return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="seconds")
    for f in session.files:
        if m := _TIME_IN_NAME.search(f.name):
            y, mo, d, h, mi, sec = (int(x) for x in m.groups())
            y = y + 2000 if y < 100 else y
            return datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc).isoformat(timespec="seconds")
    epoch_s = min(f.stat().st_mtime for f in session.files)
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="seconds")


def read_session(session: Session, cfg: Config) -> Signals:
    wanted = cfg.signal_map.all_names()
    parts = []
    for f in session.files:
        if f.suffix == ".hoot":
            parts.append(read_hoot(f, wanted, cfg.local_log_dir / CONVERTED_DIR, cfg.owlet_dirs, cfg.owlet_args))
        else:
            parts.append(read_wpilog(f, wanted))
    return merge(parts)


def as_session(path: Path) -> Session:
    if path.is_dir():
        files = sorted(f for f in path.iterdir() if f.is_file() and f.suffix in LOG_SUFFIXES)
        return Session(path.name, files)
    return Session(path.stem, [path])


MIN_ENABLED_S = 5.0  # sessions enabled for less than this (bench testing, never enabled) are skipped


def process_session(session: Session, cfg: Config, db: Database) -> int | None:
    """Returns the new log id, or None if already ingested or skipped."""
    if not session.files:
        return None
    digest = session_digest(session.files)
    if db.is_ingested(digest):
        return None

    signals = read_session(session, cfg)
    missing = sorted(n for n in cfg.signal_map.all_names()
                     if n not in signals.numeric and n not in signals.strings)
    if missing:
        log.debug("%s: signals not found: %s", session.name, ", ".join(missing))

    win = match_window(signals, cfg)
    summary = compute_match_summary(signals, cfg, win)
    if summary["enabled_s"] < MIN_ENABLED_S:
        db.mark_skipped(session.name, digest, f"enabled {summary['enabled_s']:.0f}s")
        log.info("skipped %s: robot enabled for only %.0fs", session.name, summary["enabled_s"])
        return None

    info = extract_match_info(signals, cfg, session)
    motors = compute_motor_stats(signals, cfg, win)
    log_id = db.insert_log(session.name, digest, info, summary, motors)
    log.info("ingested %s: %s %s%s, %.0fs enabled, %d motors%s",
             session.name, info["event"] or "?", info["match_type"] or "",
             f" #{info['match_number']}" if info["match_number"] else "",
             summary["enabled_s"], len(motors),
             f", robot disabled {summary['enable_drops']}x mid-match" if summary.get("enable_drops") else "")
    return log_id


def process_log(path: Path, cfg: Config, db: Database) -> int | None:
    """Ingest a single log file, or a folder holding one session's files."""
    return process_session(as_session(path), cfg, db)


def pending_sessions(cfg: Config) -> list[Session]:
    """Everything in local_log_dir, oldest first: top-level .wpilog/.hoot files, plus each
    subfolder (one CTRE session). The digest check skips ones already done, so this doubles as
    the offline queue."""
    root = cfg.local_log_dir
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if p.is_file() and p.suffix in LOG_SUFFIXES:
            out.append(Session(p.stem, [p]))
        elif p.is_dir() and p.name != CONVERTED_DIR:
            s = as_session(p)
            if s.files:
                out.append(s)
    return sorted(out, key=lambda s: min(f.stat().st_mtime for f in s.files))
