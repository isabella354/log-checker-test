"""Detect the roboRIO and pull any new log files over SFTP."""
from __future__ import annotations

import logging
import socket
import stat
import time
from pathlib import Path

import paramiko

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

LOG_EXTENSIONS = (".wpilog", ".hoot")


def find_robot(hosts: list[str], port: int = 22, timeout: float = 1.0) -> str | None:
    """Return the first host with SSH open, or None if the robot isn't reachable."""
    for host in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return host
        except OSError:
            continue
    return None


def open_sftp(host: str, user: str, password: str) -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    """The roboRIO's lvuser has an empty password; depending on image it accepts
    'none' auth or an empty password, so try both."""
    transport = paramiko.Transport((host, 22))
    transport.connect()  # host key isn't verified; this is a robot on a private network
    try:
        transport.auth_none(user)
    except paramiko.BadAuthenticationType:
        transport.auth_password(user, password)
    return transport, paramiko.SFTPClient.from_transport(transport)


def list_remote_logs(sftp: paramiko.SFTPClient, dirs: list[str]) -> list[tuple[str, str, paramiko.SFTPAttributes]]:
    """(remote path, relative path, attrs) for every log file in each dir and one level of
    subfolders. CTRE puts each session in its own folder: logs/mttd_Q12/mttd_Q12_rio_....hoot."""
    found = []
    for d in dirs:
        try:
            entries = sftp.listdir_attr(d)
        except OSError:
            continue  # folder doesn't exist (e.g. no USB stick)
        for a in entries:
            if stat.S_ISDIR(a.st_mode or 0):
                try:
                    inner = sftp.listdir_attr(f"{d}/{a.filename}")
                except OSError:
                    continue
                for b in inner:
                    if b.filename.endswith(LOG_EXTENSIONS) and stat.S_ISREG(b.st_mode or 0):
                        found.append((f"{d}/{a.filename}/{b.filename}", f"{a.filename}/{b.filename}", b))
            elif a.filename.endswith(LOG_EXTENSIONS) and stat.S_ISREG(a.st_mode or 0):
                found.append((f"{d}/{a.filename}", a.filename, a))
    return found


def diagnose(cfg: Config, db: Database) -> None:
    """Print everything pull_new_logs would see and decide, without downloading anything."""
    for host in cfg.robot_hosts:
        print(f"  {host:<28} {'open' if find_robot([host]) else 'no answer'}")
    host = find_robot(cfg.robot_hosts)
    if host is None:
        print("Robot not reachable on port 22. Check the cable/radio and the team number in config.yaml.")
        return
    try:
        transport, sftp = open_sftp(host, cfg.ssh_user, cfg.ssh_password)
    except Exception as e:
        print(f"Reached {host} but SSH login as {cfg.ssh_user!r} failed: {e!r}")
        return
    print(f"Logged in to {host} as {cfg.ssh_user}.")
    try:
        for d in cfg.remote_log_dirs:
            try:
                sftp.listdir(d)
            except OSError as e:
                print(f"\n{d}: cannot list ({e.strerror or e})")
                continue
            files = sorted(list_remote_logs(sftp, [d]), key=lambda f: f[2].st_mtime or 0)
            # group by session folder (or the file itself, for top-level files)
            groups: dict[str, list] = {}
            for remote, rel, a in files:
                groups.setdefault(rel.split("/")[0] if "/" in rel else rel, []).append((rel, a))
            print(f"\n{d}: {len(files)} log files in {len(groups)} sessions")
            for name, items in groups.items():
                size = sum(a.st_size for _, a in items) / 1e6
                new = sum(not db.was_downloaded(rel, a.st_size) for rel, a in items)
                status = "NEW" if new == len(items) else "already downloaded" if new == 0 else f"{new} new"
                print(f"  {size:>8.1f} MB  {len(items)} file(s)  {name:<45} {status}")
        print("\nNote: the newest session is usually the one being written right now; "
              "watch skips it until the robot is power-cycled.")
        print("Other places logs may be:", end=" ")
        for d in ("/home/lvuser", "/u", "/U", "/media"):
            try:
                print(f"{d}: {', '.join(sorted(sftp.listdir(d))[:15]) or '(empty)'};", end=" ")
            except OSError:
                pass
        print()
    finally:
        sftp.close()
        transport.close()


def pull_new_logs(cfg: Config, db: Database) -> list[Path]:
    host = find_robot(cfg.robot_hosts)
    if host is None:
        return []
    log.info("robot reachable at %s", host)

    cfg.local_log_dir.mkdir(parents=True, exist_ok=True)
    transport, sftp = open_sftp(host, cfg.ssh_user, cfg.ssh_password)
    downloaded: list[Path] = []
    try:
        files = list_remote_logs(sftp, cfg.remote_log_dirs)
        candidates = [f for f in files if not db.was_downloaded(f[1], f[2].st_size)]
        log.info("found %d log file(s) on robot, %d new", len(files), len(candidates))
        if not candidates:
            return []

        # The log currently being written keeps growing; skip anything whose size changes.
        time.sleep(cfg.settle_s)
        for remote, rel, a in sorted(candidates, key=lambda f: f[2].st_mtime or 0):
            try:
                size_now = sftp.stat(remote).st_size
            except OSError:
                continue  # renamed in the meantime (loggers rename files once they get match info)
            if size_now != a.st_size:
                log.info("skipping %s (still being written)", rel)
                continue

            local = cfg.local_log_dir / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + ".part")
            log.info("downloading %s (%.1f MB)", rel, a.st_size / 1e6)
            sftp.get(remote, str(tmp))
            tmp.replace(local)
            db.mark_downloaded(rel, a.st_size)
            downloaded.append(local)
    finally:
        sftp.close()
        transport.close()
    return downloaded
