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


def read_wpilog(path: Path, wanted: set[str] | None = None) -> Signals:
    """Read numeric and string entries. Pass `wanted` to skip everything else (much faster).

    A wanted name like "/Shooter/ShooterMotorStatorCurrents[2]" picks element 2 out of an
    array entry (AdvantageKit logs `double[]` input fields this way) and stores it under that name.
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
            entries[sd.entry] = (sd.name, sd.type)
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


def convert_hoot(path: Path, owlet_cmd: list[str]) -> Path:
    """Convert a Phoenix 6 .hoot file to .wpilog with CTRE's owlet CLI."""
    out_dir = path.parent / "converted"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / (path.stem + ".wpilog")
    cmd = [part.format(input=str(path), output=str(out)) for part in owlet_cmd]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    if not out.exists():
        raise RuntimeError(f"owlet ran but produced no output: {' '.join(cmd)}")
    return out
