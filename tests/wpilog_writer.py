"""Minimal .wpilog writer for test fixtures (robotpy's wpiutil only ships the reader).

Format: https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/doc/datalog.adoc
"""
import struct

_HDR = 0x7F  # 4-byte entry id, 4-byte payload size, 8-byte timestamp


class WpilogWriter:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.f.write(b"WPILOG" + struct.pack("<HI", 0x0100, 0))
        self.ids: dict[str, tuple[int, str]] = {}

    def _record(self, entry: int, ts_s: float, payload: bytes) -> None:
        self.f.write(struct.pack("<BIIQ", _HDR, entry, len(payload), int(ts_s * 1e6)) + payload)

    def _start(self, name: str, typ: str, ts_s: float) -> int:
        entry = len(self.ids) + 1
        n, t = name.encode(), typ.encode()
        payload = (struct.pack("<BI", 0, entry) + struct.pack("<I", len(n)) + n
                   + struct.pack("<I", len(t)) + t + struct.pack("<I", 0))
        self._record(0, ts_s, payload)
        self.ids[name] = (entry, typ)
        return entry

    def append(self, name: str, typ: str, ts_s: float, value) -> None:
        if name not in self.ids:
            self._start(name, typ, ts_s)
        entry, typ = self.ids[name]
        payload = {
            "double": lambda v: struct.pack("<d", v),
            "int64": lambda v: struct.pack("<q", v),
            "boolean": lambda v: struct.pack("<?", v),
            "string": lambda v: v.encode(),
        }[typ](value)
        self._record(entry, ts_s, payload)

    def close(self) -> None:
        self.f.close()
