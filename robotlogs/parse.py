"""Read .wpilog files (and .hoot via owlet) into plain numpy series."""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from wpiutil.log import DataLogReader

from .metrics import Series

_NUMERIC_TYPES = {"double", "float", "int64", "boolean"}
_ARRAY_TYPES = {"double[]": "getDoubleArray", "float[]": "getFloatArray", "int64[]": "getIntegerArray"}
_INDEXED = re.compile(r"^(.*)\[(\d+)\]$")  # "/Shooter/ShooterMotorStatorCurrents[2]"


@dataclass
class Signals:
    numeric: dict[str, Series] = field(default_factory=dict)
    strings: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    start_s: float = 0.0
    end_s: float = 0.0

    def last_string(self, name: str | None) -> str | None:
        vals = self.strings.get(name) if name else None
        return vals[-1][1] if vals else None

    def last_number(self, name: str | None) -> float | None:
        s = self.numeric.get(name) if name else None
        return float(s.v[-1]) if s is not None and len(s.v) else None


def _decode(record, typ: str):
    if typ == "double":
        return record.getDouble()
    if typ == "float":
        return record.getFloat()
    if typ == "int64":
        return record.getInteger()
    if typ == "boolean":
        return float(record.getBoolean())
    if typ == "string":
        return record.getString()
    return None  # arrays, structs, protobuf: not needed for summaries yet


def _open(path: Path) -> DataLogReader:
    reader = DataLogReader(str(path))
    if not reader.isValid():
        raise ValueError(f"{path} is not a valid wpilog")
    return reader


def read_wpilog(path: Path, wanted: set[str] | None = None, prefix: str = "") -> Signals:
    """Read numeric and string entries. Pass `wanted` to skip everything else (much faster).

    A wanted name like "/Shooter/ShooterMotorStatorCurrents[2]" picks element 2 out of an
    array entry (AdvantageKit logs `double[]` input fields this way) and stores it under that name.

    `prefix` renames every entry to "<prefix>/<name>" with any leading "Phoenix6/" dropped; used
    for converted .hoot files so "Phoenix6/TalonFX-4/StatorCurrent" from the CANivore becomes
    "canivore/TalonFX-4/StatorCurrent" and can't collide with TalonFX-4 on the rio bus.
    """
    # array base name -> [(index, wanted name)]
    indexed: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for w in wanted or ():
        if m := _INDEXED.match(w):
            indexed[m.group(1)].append((int(m.group(2)), w))
    entries: dict[int, tuple[str, str]] = {}
    nums: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    strs: dict[str, list] = defaultdict(list)
    t_min, t_max = float("inf"), float("-inf")

    for rec in _open(path):
        if rec.isStart():
            sd = rec.getStartData()
            name = f"{prefix}/{sd.name.removeprefix('Phoenix6/')}" if prefix else sd.name
            entries[sd.entry] = (name, sd.type)
            continue
        if rec.isControl():
            continue
        entry = entries.get(rec.getEntry())
        if entry is None:
            continue
        ts = rec.getTimestamp() / 1e6
        t_min, t_max = min(t_min, ts), max(t_max, ts)

        name, typ = entry
        if typ in _ARRAY_TYPES and name in indexed:
            try:
                arr = getattr(rec, _ARRAY_TYPES[typ])()
            except Exception:
                continue
            for idx, wname in indexed[name]:
                if idx < len(arr):
                    ts_list, v_list = nums[wname]
                    ts_list.append(ts)
                    v_list.append(float(arr[idx]))
            continue
        if wanted is not None and name not in wanted:
            continue
        if typ not in _NUMERIC_TYPES and typ != "string":
            continue
        try:
            val = _decode(rec, typ)
        except Exception:
            continue  # malformed record; skip rather than abort the whole match
        if typ == "string":
            strs[name].append((ts, val))
        else:
            ts_list, v_list = nums[name]
            ts_list.append(ts)
            v_list.append(val)

    numeric = {
        n: Series(np.asarray(t, dtype=float), np.asarray(v, dtype=float))
        for n, (t, v) in nums.items()
    }
    if t_min == float("inf"):
        t_min = t_max = 0.0
    return Signals(numeric=numeric, strings=dict(strs), start_s=t_min, end_s=t_max)


def list_signals(path: Path) -> list[tuple[str, str, int]]:
    """(name, type, record count) for every entry in a log — use this to fill in signal_map."""
    entries: dict[int, tuple[str, str]] = {}
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for rec in _open(path):
        if rec.isStart():
            sd = rec.getStartData()
            entries[sd.entry] = (sd.name, sd.type)
            counts.setdefault((sd.name, sd.type), 0)
        elif not rec.isControl() and rec.getEntry() in entries:
            counts[entries[rec.getEntry()]] += 1
    return sorted((n, t, c) for (n, t), c in counts.items())


def merge(parts: list[Signals]) -> Signals:
    """Combine signals from several files of the same session (e.g. rio + CANivore hoot files)."""
    out = Signals(start_s=min(p.start_s for p in parts), end_s=max(p.end_s for p in parts))
    for p in parts:
        out.numeric.update(p.numeric)
        out.strings.update(p.strings)
    return out


# ---------- CTRE .hoot ----------

_OWLET_NAME = re.compile(r"^owlet-.*-C(\d+)(\.exe)?$")


def hoot_bus(path: Path) -> str:
    """CTRE names each bus's file: rio_<time>.hoot for the roboRIO bus, <CANivore serial>_<time>.hoot."""
    return "rio" if re.search(r"(^|_)rio_", path.name) else "canivore"


def find_owlets(dirs: list[Path]) -> dict[int, Path]:
    """Map hoot format version (the C<n> in owlet-26.3.0-C19) to an owlet binary."""
    found: dict[int, Path] = {}
    for d in dirs:
        if d.is_dir():
            for f in d.iterdir():
                if (m := _OWLET_NAME.match(f.name)) and f.is_file():
                    found.setdefault(int(m.group(1)), f)
    return found


def convert_hoot(path: Path, out_dir: Path, owlet_dirs: list[Path], owlet_args: list[str]) -> Path:
    """Convert a Phoenix 6 .hoot file to .wpilog with the owlet version that matches its format."""
    owlets = find_owlets(owlet_dirs)
    if not owlets:
        raise RuntimeError(
            "No CTRE owlet converter found. Install AdvantageScope and open any .hoot file from the "
            f"robot once (it downloads the right converter), or add a folder to owlet_dirs. Looked in: "
            + ", ".join(map(str, owlet_dirs)))
    probe = subprocess.run([str(owlets[max(owlets)]), str(path), "--compliancy"],
                           capture_output=True, text=True)
    m = re.search(r"hoot log:\s*(\d+)", probe.stdout + probe.stderr)
    if not m:
        raise RuntimeError(f"couldn't read hoot version of {path.name}: {probe.stdout}{probe.stderr}")
    version = int(m.group(1))
    if version not in owlets:
        raise RuntimeError(
            f"{path.name} needs owlet C{version}, have C{sorted(owlets)}. Open this .hoot file in "
            "AdvantageScope once so it downloads the matching converter.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (path.stem + ".wpilog")
    cmd = [str(owlets[version]), str(path), str(out), *owlet_args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"owlet failed on {path.name}: {r.stdout}{r.stderr}")
    return out


def read_hoot(path: Path, wanted: set[str] | None, tmp_dir: Path,
              owlet_dirs: list[Path], owlet_args: list[str]) -> Signals:
    """Convert, read with a bus prefix, then delete the (large) converted file."""
    out = convert_hoot(path, tmp_dir, owlet_dirs, owlet_args)
    try:
        return read_wpilog(out, wanted, prefix=hoot_bus(path))
    finally:
        out.unlink(missing_ok=True)
