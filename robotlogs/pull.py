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


def pull_new_logs(cfg: Config, db: Database) -> list[Path]:
    host = find_robot(cfg.robot_hosts)
    if host is None:
        return []
    log.info("robot reachable at %s", host)

    cfg.local_log_dir.mkdir(parents=True, exist_ok=True)
    transport, sftp = open_sftp(host, cfg.ssh_user, cfg.ssh_password)
    downloaded: list[Path] = []
    try:
        candidates: list[tuple[str, paramiko.SFTPAttributes]] = []
        for d in cfg.remote_log_dirs:
            try:
                entries = sftp.listdir_attr(d)
            except OSError:
                continue  # directory doesn't exist (e.g. no USB stick)
            for a in entries:
                if (
                    a.filename.endswith(LOG_EXTENSIONS)
                    and stat.S_ISREG(a.st_mode or 0)
                    and not db.was_downloaded(a.filename, a.st_size)
                ):
                    candidates.append((d, a))

        if not candidates:
            return []

        # The log currently being written keeps growing; skip anything whose size changes.
        time.sleep(cfg.settle_s)
        for d, a in candidates:
            remote = f"{d}/{a.filename}"
            try:
                size_now = sftp.stat(remote).st_size
            except OSError:
                continue  # renamed in the meantime (DataLogManager renames FRC_TBD_* once it has match info)
            if size_now != a.st_size:
                log.info("skipping %s (still being written)", a.filename)
                continue

            local = cfg.local_log_dir / a.filename
            tmp = local.with_name(local.name + ".part")
            log.info("downloading %s (%.1f MB)", a.filename, a.st_size / 1e6)
            sftp.get(remote, str(tmp))
            tmp.replace(local)
            db.mark_downloaded(a.filename, a.st_size)
            downloaded.append(local)
    finally:
        sftp.close()
        transport.close()
    return downloaded
