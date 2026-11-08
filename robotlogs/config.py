from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class MotorSignals:
    supply: str | None = None
    stator: str | None = None
    temp: str | None = None


@dataclass
class SignalMap:
    enabled: str | None = "DS:enabled"
    system_time: str | None = "systemTime"
    event_name: str | None = "NT:/FMSInfo/EventName"
    match_type: str | None = "NT:/FMSInfo/MatchType"
    match_number: str | None = "NT:/FMSInfo/MatchNumber"
    battery_id: str | None = None
    battery_voltage: str | list[str] | None = None  # a list is averaged
    total_current: str | None = None
    brownout: str | None = None
    motors: dict[str, MotorSignals] = field(default_factory=dict)

    def all_names(self) -> set[str]:
        names = {
            self.enabled, self.system_time, self.event_name, self.match_type,
            self.match_number, self.battery_id, self.total_current, self.brownout,
        }
        names |= set(self.battery_voltages())
        for m in self.motors.values():
            names |= {m.supply, m.stator, m.temp}
        names.discard(None)
        return names

    def battery_voltages(self) -> list[str]:
        bv = self.battery_voltage
        return [] if not bv else [bv] if isinstance(bv, str) else list(bv)


@dataclass
class Thresholds:
    low_voltage: float = 8.0
    critical_voltage: float = 7.0
    hot_motor_c: float = 70.0


@dataclass
class AlertConfig:
    history_matches: int = 20
    min_history: int = 5
    z_threshold: float = 2.0
    wear_slope_per_match: float = 0.02


@dataclass
class Config:
    robot_hosts: list[str]
    ssh_user: str
    ssh_password: str
    remote_log_dirs: list[str]
    local_log_dir: Path
    db_path: Path
    poll_interval_s: float
    settle_s: float
    sample_rate_hz: float
    owlet_dirs: list[Path]   # folders holding owlet-<ver>-C<n> binaries; matched to each hoot file
    owlet_args: list[str]
    signal_map: SignalMap
    thresholds: Thresholds
    alerts: AlertConfig


def default_owlet_dirs() -> list[Path]:
    """Where AdvantageScope keeps the owlet versions it downloads (it fetches the right one the
    first time you open a .hoot file in it)."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [base / "AdvantageScope" / "owlet"]


def team_hosts(team: int) -> list[str]:
    """Addresses to try, fastest/most specific first: USB, static Ethernet IP, mDNS."""
    te, am = divmod(int(team), 100)
    return ["172.22.11.2", f"10.{te}.{am}.2", f"roborio-{team}-frc.local"]


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    base = path.parent

    hosts = raw.get("robot_hosts")
    if not hosts:
        if "team" not in raw:
            raise ValueError("config needs either `team` or `robot_hosts`")
        hosts = team_hosts(raw["team"])

    sm_raw = dict(raw.get("signal_map") or {})
    motors = {
        name: MotorSignals(**(m or {}))
        for name, m in (sm_raw.pop("motors", None) or {}).items()
    }

    return Config(
        robot_hosts=list(hosts),
        ssh_user=raw.get("ssh_user", "lvuser"),
        ssh_password=raw.get("ssh_password", ""),
        remote_log_dirs=list(raw.get("remote_log_dirs", ["/home/lvuser/logs"])),
        local_log_dir=(base / raw.get("local_log_dir", "logs")).resolve(),
        db_path=(base / raw.get("db_path", "robot_trends.db")).resolve(),
        poll_interval_s=float(raw.get("poll_interval_s", 10)),
        settle_s=float(raw.get("settle_s", 2.0)),
        sample_rate_hz=float(raw.get("sample_rate_hz", 50)),
        owlet_dirs=[(base / Path(d).expanduser()).resolve() for d in raw.get("owlet_dirs") or []]
        + default_owlet_dirs(),
        owlet_args=list(raw.get("owlet_args", ["-f", "wpilog"])),
        signal_map=SignalMap(**sm_raw, motors=motors),
        thresholds=Thresholds(**(raw.get("thresholds") or {})),
        alerts=AlertConfig(**(raw.get("alerts") or {})),
    )
