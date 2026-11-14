#!/usr/bin/env python3
"""
Privileged scheduler controller for oVirt Backup.

The web console remains unprivileged. This controller owns the narrow set of
root operations needed to configure and enable/disable ovirt-backup.timer.
Requests arrive over a Unix socket writable only by root and group ovirt-backup.
"""

import argparse
import configparser
import grp
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

SOCKET_PATH = Path("/run/ovirt-backup/scheduler.sock")
CONFIG_PATH = Path("/etc/ovirt-backup/scheduler.json")
DROPIN_DIR = Path("/etc/systemd/system/ovirt-backup.timer.d")
DROPIN_PATH = DROPIN_DIR / "10-schedule.conf"
TIMER_UNIT = "ovirt-backup.timer"
SERVICE_UNIT = "ovirt-backup.service"
BACKUP_CONFIG_PATH = Path("/etc/ovirt-backup/config/backup.cfg")
GROUP = "ovirt-backup"
MAX_REQUEST = 64 * 1024

DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_SYSTEMD = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}
DEFAULT_CONFIG = {
    "enabled": False,
    "time": "02:00",
    "weekdays": list(DAY_ORDER),
    "timezone": "UTC",
    "randomized_delay_seconds": 0,
}


class SchedulerError(RuntimeError):
    pass


def run(args, check=False):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SchedulerError(
            "%s failed (%d): %s"
            % (" ".join(args), result.returncode, result.stdout.strip())
        )
    return result


def atomic_write(path, text, mode=0o640, uid=0, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.chown(tmp, uid, gid)
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def normalize_config(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SchedulerError("scheduler configuration must be a JSON object")

    unknown = set(raw) - set(DEFAULT_CONFIG)
    if unknown:
        raise SchedulerError(
            "unknown scheduler setting(s): %s" % ", ".join(sorted(unknown))
        )

    out = dict(DEFAULT_CONFIG)
    out.update(raw)

    if not isinstance(out["enabled"], bool):
        raise SchedulerError("enabled must be true or false")

    time_value = str(out["time"]).strip()
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", time_value)
    if not match:
        raise SchedulerError("time must use 24-hour HH:MM format")
    out["time"] = time_value

    weekdays = out["weekdays"]
    if not isinstance(weekdays, list) or not weekdays:
        raise SchedulerError("weekdays must contain at least one day")
    normalized_days = []
    for item in weekdays:
        day = str(item).strip().lower()
        if day not in DAY_SYSTEMD:
            raise SchedulerError("invalid weekday: %s" % item)
        if day not in normalized_days:
            normalized_days.append(day)
    out["weekdays"] = [
        day for day in DAY_ORDER if day in normalized_days
    ]

    timezone = str(out["timezone"]).strip()
    if (
        not timezone
        or timezone.startswith("/")
        or ".." in timezone.split("/")
        or not (Path("/usr/share/zoneinfo") / timezone).is_file()
    ):
        raise SchedulerError("unknown timezone: %s" % timezone)
    out["timezone"] = timezone

    delay = out["randomized_delay_seconds"]
    if isinstance(delay, bool):
        raise SchedulerError("randomized_delay_seconds must be an integer")
    try:
        delay = int(delay)
    except (TypeError, ValueError):
        raise SchedulerError("randomized_delay_seconds must be an integer")
    if not 0 <= delay <= 3600:
        raise SchedulerError(
            "randomized_delay_seconds must be between 0 and 3600"
        )
    out["randomized_delay_seconds"] = delay

    return out


def calendar_expression(config):
    days = config["weekdays"]
    prefix = "" if days == DAY_ORDER else ",".join(
        DAY_SYSTEMD[day] for day in days
    ) + " "
    return "%s*-*-* %s:00 %s" % (
        prefix,
        config["time"],
        config["timezone"],
    )


def validate_calendar(expr):
    result = run([
        "systemd-analyze",
        "calendar",
        expr,
    ])
    if result.returncode != 0:
        raise SchedulerError(
            "systemd rejected calendar expression %r: %s"
            % (expr, result.stdout.strip())
        )
    return result.stdout.strip()


def render_dropin(config):
    expr = calendar_expression(config)
    return (
        "# Managed by oVirt Backup Console. Do not edit by hand.\n"
        "[Timer]\n"
        "OnCalendar=\n"
        "OnCalendar=%s\n"
        "Persistent=true\n"
        "RandomizedDelaySec=%d\n"
        % (expr, config["randomized_delay_seconds"])
    )


def read_config():
    if not CONFIG_PATH.is_file():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SchedulerError(
            "cannot read scheduler config %s: %s" % (CONFIG_PATH, exc)
        ) from exc
    return normalize_config(raw)


def systemctl_value(unit, prop):
    result = run(["systemctl", "show", unit, "-p", prop, "--value"])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def systemctl_state():
    enabled_result = run(["systemctl", "is-enabled", TIMER_UNIT])
    active_result = run(["systemctl", "is-active", TIMER_UNIT])
    return {
        "enabled": enabled_result.returncode == 0,
        "enabled_state": enabled_result.stdout.strip() or "unknown",
        "active": active_result.returncode == 0,
        "active_state": active_result.stdout.strip() or "unknown",
        "sub_state": systemctl_value(TIMER_UNIT, "SubState"),
        "next_elapse": systemctl_value(
            TIMER_UNIT,
            "NextElapseUSecRealtime",
        ),
        "last_trigger": systemctl_value(TIMER_UNIT, "LastTriggerUSec"),
        "service_active_state": systemctl_value(
            SERVICE_UNIT,
            "ActiveState",
        ),
        "service_result": systemctl_value(SERVICE_UNIT, "Result"),
    }


def backup_config_guard():
    if not BACKUP_CONFIG_PATH.is_file():
        return {
            "ok": False,
            "path": str(BACKUP_CONFIG_PATH),
            "dry_run": None,
            "reason": "canonical backup configuration is missing",
        }

    cp = configparser.RawConfigParser()
    try:
        with open(BACKUP_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cp.read_file(fh)
        if not cp.has_section("config"):
            raise SchedulerError("missing [config] section")
        dry_run = cp.getboolean("config", "dry_run", fallback=False)
    except Exception as exc:
        return {
            "ok": False,
            "path": str(BACKUP_CONFIG_PATH),
            "dry_run": None,
            "reason": "cannot parse canonical backup configuration: %s" % exc,
        }

    if dry_run:
        return {
            "ok": False,
            "path": str(BACKUP_CONFIG_PATH),
            "dry_run": True,
            "reason": "canonical backup configuration has dry_run=True",
        }

    return {
        "ok": True,
        "path": str(BACKUP_CONFIG_PATH),
        "dry_run": False,
        "reason": None,
    }


def legacy_cron_entries():
    paths = []
    for candidate in (Path("/etc/crontab"),):
        if candidate.is_file():
            paths.append(candidate)
    cron_d = Path("/etc/cron.d")
    if cron_d.is_dir():
        paths.extend(sorted(p for p in cron_d.iterdir() if p.is_file()))
    for root in (Path("/var/spool/cron"), Path("/var/spool/cron/crontabs")):
        if root.is_dir():
            paths.extend(sorted(p for p in root.iterdir() if p.is_file()))

    seen = set()
    entries = []
    for path in paths:
        try:
            real = path.resolve()
        except Exception:
            real = path
        key = str(real)
        if key in seen:
            continue
        seen.add(key)
        try:
            lines = path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except (OSError, PermissionError):
            continue
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "backup_native.py" in stripped:
                continue
            if re.search(r"(^|[ /])backup\.py(?:\s|$)", stripped):
                entries.append({
                    "source": str(path),
                    "line_number": number,
                    "line": stripped,
                })
    return entries


def legacy_processes():
    result = []
    proc = Path("/proc")
    if not proc.is_dir():
        return result
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if not raw:
            continue
        command = raw.replace(b"\x00", b" ").decode(
            "utf-8",
            errors="replace",
        ).strip()
        if not command or "backup_native.py" in command:
            continue
        if re.search(r"(^|[ /])backup\.py(?:\s|$)", command):
            result.append({
                "pid": int(entry.name),
                "command": command,
            })
    return sorted(result, key=lambda item: item["pid"])


def status():
    config = read_config()
    expr = calendar_expression(config)
    calendar_detail = None
    calendar_error = None
    try:
        calendar_detail = validate_calendar(expr)
    except Exception as exc:
        calendar_error = str(exc)

    cron = legacy_cron_entries()
    processes = legacy_processes()
    backup_guard = backup_config_guard()
    timer = systemctl_state()
    return {
        "ok": calendar_error is None,
        "config": config,
        "calendar": expr,
        "calendar_detail": calendar_detail,
        "calendar_error": calendar_error,
        "timer": timer,
        "in_sync": bool(config["enabled"]) == bool(timer["enabled"]),
        "legacy_cron_entries": cron,
        "legacy_processes": processes,
        "migration_guard": {
            "blocked": bool(cron or processes or not backup_guard["ok"]),
            "reason": (
                "legacy cron/process detected"
                if cron or processes
                else backup_guard["reason"]
            ),
        },
        "backup_config_guard": backup_guard,
        "paths": {
            "config": str(CONFIG_PATH),
            "dropin": str(DROPIN_PATH),
            "timer_unit": TIMER_UNIT,
            "service_unit": SERVICE_UNIT,
        },
    }


def validate_payload(payload):
    config = normalize_config(payload)
    expr = calendar_expression(config)
    detail = validate_calendar(expr)
    cron = legacy_cron_entries()
    processes = legacy_processes()
    backup_guard = backup_config_guard()
    return {
        "ok": True,
        "config": config,
        "calendar": expr,
        "calendar_detail": detail,
        "migration_guard": {
            "blocked": bool(cron or processes or not backup_guard["ok"]),
            "legacy_cron_entries": cron,
            "legacy_processes": processes,
            "backup_config_guard": backup_guard,
        },
    }


def apply_payload(payload):
    config = normalize_config(payload)
    validated = validate_payload(config)
    guard = validated["migration_guard"]

    if config["enabled"] and guard["blocked"]:
        parts = []
        if guard.get("legacy_cron_entries") or guard.get("legacy_processes"):
            parts.append(
                "legacy backup cron/process is present; complete cutover first"
            )
        backup_guard = guard.get("backup_config_guard") or {}
        if not backup_guard.get("ok", True):
            parts.append(
                backup_guard.get("reason")
                or "canonical backup configuration is not ready"
            )
        raise SchedulerError(
            "native timer cannot be enabled: %s"
            % "; ".join(parts or ["migration guard is blocking enablement"])
        )

    gid = grp.getgrnam(GROUP).gr_gid
    atomic_write(
        CONFIG_PATH,
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        mode=0o640,
        uid=0,
        gid=gid,
    )
    atomic_write(
        DROPIN_PATH,
        render_dropin(config),
        mode=0o644,
        uid=0,
        gid=0,
    )

    run(["systemctl", "daemon-reload"], check=True)
    if config["enabled"]:
        run(["systemctl", "enable", "--now", TIMER_UNIT], check=True)
    else:
        # Disabling the native timer never modifies the legacy cron.
        run(["systemctl", "disable", "--now", TIMER_UNIT], check=False)

    result = status()
    result["applied"] = True
    return result


def handle_request(request):
    if not isinstance(request, dict):
        raise SchedulerError("request must be a JSON object")
    action = request.get("action")
    if action == "status":
        return status()
    if action == "validate":
        return validate_payload(request.get("config"))
    if action == "apply":
        return apply_payload(request.get("config"))
    raise SchedulerError("unknown action: %r" % action)


def send_json(conn, payload):
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    conn.sendall(data)


def serve(socket_path):
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    group = grp.getgrnam(GROUP)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chown(socket_path, 0, group.gr_gid)
    os.chmod(socket_path, 0o660)
    server.listen(8)

    stopping = {"value": False}

    def stop(_signum, _frame):
        stopping["value"] = True
        try:
            server.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while not stopping["value"]:
            try:
                conn, _addr = server.accept()
            except OSError:
                if stopping["value"]:
                    break
                raise
            with conn:
                try:
                    chunks = []
                    total = 0
                    while True:
                        chunk = conn.recv(min(8192, MAX_REQUEST - total + 1))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > MAX_REQUEST:
                            raise SchedulerError("request too large")
                        if b"\n" in chunk:
                            break
                    raw = b"".join(chunks).split(b"\n", 1)[0]
                    if not raw:
                        raise SchedulerError("empty request")
                    request = json.loads(raw.decode("utf-8"))
                    response = handle_request(request)
                    send_json(conn, {"ok": True, "result": response})
                except Exception as exc:
                    send_json(conn, {
                        "ok": False,
                        "error": str(exc),
                    })
    finally:
        try:
            server.close()
        finally:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass


def self_test():
    tests = []

    def check(name, condition):
        tests.append({"name": name, "ok": bool(condition)})
        if not condition:
            raise AssertionError(name)

    cfg = normalize_config({
        "enabled": False,
        "time": "03:15",
        "weekdays": ["mon", "wed", "fri"],
        "timezone": "UTC",
        "randomized_delay_seconds": 30,
    })
    check("normalize", cfg["time"] == "03:15")
    check(
        "calendar",
        calendar_expression(cfg)
        == "Mon,Wed,Fri *-*-* 03:15:00 UTC",
    )
    dropin = render_dropin(cfg)
    check("reset OnCalendar", "OnCalendar=\n" in dropin)
    check("persistent", "Persistent=true" in dropin)

    try:
        normalize_config({**cfg, "time": "25:00"})
    except SchedulerError:
        invalid_time = True
    else:
        invalid_time = False
    check("invalid time rejected", invalid_time)

    print(json.dumps({"ok": True, "checks": tests}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("serve", "status", "validate", "apply", "self-test"),
    )
    parser.add_argument("--socket", default=str(SOCKET_PATH))
    args = parser.parse_args()

    if args.command == "serve":
        if os.geteuid() != 0:
            raise SystemExit("serve must run as root")
        serve(args.socket)
        return 0

    if args.command == "self-test":
        return self_test()

    if args.command == "status":
        print(json.dumps(status(), indent=2, sort_keys=True))
        return 0

    payload = json.load(sys.stdin)
    result = (
        validate_payload(payload)
        if args.command == "validate"
        else apply_payload(payload)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SchedulerError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)
