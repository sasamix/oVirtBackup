#!/usr/bin/env python3
import argparse
import configparser
import hmac
import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ovirtsdk4 as sdk
import pycurl

LOG = logging.getLogger("ovirt-backup-web")
MAX_VMS = 400

SENSITIVE_KEYS = {"password", "passwd", "token", "secret", "credential", "private_key", "api_key"}
DEFAULT_CONFIG_BACKUP_DIR = "/var/lib/ovirt-backup/config-backups"
DEFAULT_OPERATION_LOCK = "/var/lib/ovirt-backup/operation.lock"
DEFAULT_SCHEDULER_SOCKET = "/run/ovirt-backup/scheduler.sock"
CONSOLE_VERSION = "0.5.1"
SESSION_COOKIE = "ovirt_backup_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60
SESSION_BROWSER_AGE = 12 * 60 * 60

# Ordered schema used by both validation and the browser settings form.
# Values are stored in the existing INI syntax expected by backup_native.py.
SETTINGS_SCHEMA = {
    "vm_names": {
        "group": "Virtual machines",
        "label": "Selected VMs",
        "options_source": "vms",
        "type": "list",
        "default": [],
        "description": "Explicit VM names. Use one selection mode only.",
    },
    "vm_names_skip": {
        "group": "Virtual machines",
        "label": "Exclude VMs",
        "options_source": "vms",
        "type": "list",
        "default": [],
        "description": "Back up every VM except these names.",
    },
    "vm_tag": {
        "group": "Virtual machines",
        "label": "VM tag",
        "options_source": "tags",
        "type": "string",
        "default": "",
        "description": "Back up VMs with this oVirt tag.",
    },
    "all_vms": {
        "group": "Virtual machines",
        "label": "All VMs",
        "type": "bool",
        "default": False,
        "description": "Back up all VMs returned by Engine.",
    },
    "server": {
        "group": "Engine",
        "label": "Engine API URL",
        "type": "string",
        "required": True,
        "description": "Example: https://engine.example/ovirt-engine/api",
    },
    "username": {
        "group": "Engine",
        "label": "Username",
        "type": "string",
        "required": True,
    },
    "password": {
        "group": "Engine",
        "label": "Password",
        "type": "secret",
        "required": True,
        "description": "Leave blank in the UI to keep the stored password.",
    },
    "backup_path": {
        "group": "Repository",
        "label": "Backup path",
        "type": "path",
        "required": True,
        "description": "Normal mounted filesystem/NFS path, not an oVirt Export Domain.",
    },
    "backup_path_must_be_mount": {
        "group": "Repository",
        "label": "Require mount point",
        "type": "bool",
        "default": True,
    },
    "storage_space_threshold": {
        "group": "Repository",
        "label": "Free-space margin",
        "type": "float",
        "default": 0.1,
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "description": "0.10 means provisioned size + 10% safety margin.",
    },
    "ca_file": {
        "group": "TLS",
        "label": "CA file",
        "type": "path",
        "default": "/etc/pki/ovirt-engine/ca.pem",
    },
    "verify_tls": {
        "group": "TLS",
        "label": "Verify TLS",
        "type": "bool",
        "default": True,
    },
    "require_consistency": {
        "group": "Backup",
        "label": "Require guest consistency",
        "type": "bool",
        "default": False,
        "description": "Fail if guest freeze/consistency cannot be obtained.",
    },
    "imageio_network": {
        "group": "Image transfer",
        "label": "ImageIO logical network",
        "options_source": "networks",
        "type": "string",
        "default": "",
        "description": "Logical network used for direct host ImageTransfer traffic.",
    },
    "imageio_max_workers": {
        "group": "Image transfer",
        "label": "Download workers",
        "type": "int",
        "default": 4,
        "min": 1,
        "max": 32,
    },
    "imageio_timeout": {
        "group": "Image transfer",
        "label": "ImageTransfer timeout",
        "type": "int",
        "default": 120,
        "min": 10,
        "max": 3600,
        "unit": "seconds",
    },
    "imageio_http_read_timeout": {
        "group": "Image transfer",
        "label": "ImageIO HTTP read timeout",
        "type": "int",
        "default": 300,
        "min": 10,
        "max": 3600,
        "unit": "seconds",
        "advanced": True,
        "description": (
            "Socket read timeout used by the ImageIO HTTP backend. "
            "Large-disk EXTENTS requests can exceed the upstream 60 second default."
        ),
    },
    "imageio_timeout_policy": {
        "group": "Image transfer",
        "label": "Timeout policy",
        "type": "choice",
        "default": "cancel",
        "choices": ["legacy", "pause", "cancel"],
    },
    "imageio_progress_interval": {
        "group": "Image transfer",
        "label": "Progress log interval",
        "type": "int",
        "default": 10,
        "min": 1,
        "max": 300,
        "unit": "seconds",
    },
    "imageio_progress_percent": {
        "group": "Image transfer",
        "label": "Progress percentage step",
        "type": "int",
        "default": 5,
        "min": 1,
        "max": 100,
        "unit": "%",
    },
    "backup_operation_timeout": {
        "group": "Backup",
        "label": "Backup operation timeout",
        "type": "int",
        "default": 3600,
        "min": 60,
        "max": 86400,
        "unit": "seconds",
    },
    "backup_keep_count": {
        "group": "Retention",
        "label": "Keep by age",
        "type": "optional_int",
        "default": 3,
        "min": 1,
        "max": 3650,
        "unit": "days",
        "description": "Blank disables age-based deletion.",
    },
    "backup_keep_count_by_number": {
        "group": "Retention",
        "label": "Keep by count",
        "type": "optional_int",
        "default": 3,
        "min": 1,
        "max": 1000,
        "description": "Blank disables count-based deletion.",
    },
    "bootable_only": {
        "group": "Disks",
        "label": "Bootable disk only",
        "type": "bool",
        "default": False,
    },
    "with_disks_deactivated": {
        "group": "Disks",
        "label": "Include deactivated disks",
        "type": "bool",
        "default": False,
    },
    "disks_id_exclude": {
        "group": "Disks",
        "label": "Excluded disk IDs",
        "options_source": "disks",
        "type": "list",
        "default": [],
    },
    "cluster_name": {
        "group": "Restore defaults",
        "label": "Target cluster",
        "options_source": "clusters",
        "type": "string",
        "default": "Default",
    },
    "storage_domain": {
        "group": "Restore defaults",
        "label": "Target storage domain",
        "options_source": "storage_domains",
        "type": "string",
        "default": "",
        "description": "Default only; restore can override it.",
    },
    "logger_fmt": {
        "group": "Logging",
        "label": "Log format",
        "type": "string",
        "default": "%(asctime)s: %(message)s",
        "advanced": True,
    },
    "native_logger_file_path": {
        "group": "Logging",
        "label": "Backup log",
        "type": "path",
        "default": "/var/log/ovirt-backup/backup-native.log",
    },
    "native_restore_logger_file_path": {
        "group": "Logging",
        "label": "Restore log",
        "type": "path",
        "default": "/var/log/ovirt-backup/restore-native.log",
    },
    "dry_run": {
        "group": "Safety",
        "label": "Force dry-run",
        "type": "bool",
        "default": False,
        "description": "When enabled, even service/manual backup invocations do not create backups.",
    },
}


def parse_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError("expected boolean")


def parse_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("expected JSON array")
            items = parsed
        else:
            items = re.split(r"[\r\n,]+", text)
    result = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def setting_default(key):
    spec = SETTINGS_SCHEMA[key]
    return spec.get("default", "")


def coerce_setting(key, value):
    spec = SETTINGS_SCHEMA[key]
    kind = spec["type"]

    if kind == "secret":
        return "" if value is None else str(value)
    if kind in ("string", "path"):
        return "" if value is None else str(value).strip()
    if kind == "bool":
        return parse_bool(value)
    if kind == "list":
        return parse_list(value)
    if kind == "choice":
        text = str(value).strip().lower()
        if text not in spec["choices"]:
            raise ValueError("must be one of: %s" % ", ".join(spec["choices"]))
        return text
    if kind in ("int", "optional_int"):
        if value is None or str(value).strip() == "":
            if kind == "optional_int":
                return ""
            raise ValueError("value is required")
        parsed = int(str(value).strip())
    elif kind == "float":
        parsed = float(str(value).strip())
    else:
        raise ValueError("unsupported setting type %s" % kind)

    if "min" in spec and parsed < spec["min"]:
        raise ValueError("must be >= %s" % spec["min"])
    if "max" in spec and parsed > spec["max"]:
        raise ValueError("must be <= %s" % spec["max"])
    return parsed


def serialize_setting(key, value):
    kind = SETTINGS_SCHEMA[key]["type"]
    if kind == "bool":
        return "True" if value else "False"
    if kind == "list":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if kind == "float":
        return ("%f" % value).rstrip("0").rstrip(".")
    return str(value)


def current_settings(cp):
    values = {}
    secret_set = {}
    for key, spec in SETTINGS_SCHEMA.items():
        raw = cp.get("config", key, fallback=None)
        if raw is None and key == "native_logger_file_path":
            raw = cp.get("config", "logger_file_path", fallback=None)
        if spec["type"] == "secret":
            secret_set[key] = bool(raw)
            values[key] = ""
            continue
        if raw is None:
            values[key] = setting_default(key)
            continue
        try:
            values[key] = coerce_setting(key, raw)
        except Exception:
            # Keep malformed existing text visible so validation can explain it.
            values[key] = raw
    return values, secret_set


def merge_settings(cp, updates):
    if not isinstance(updates, dict):
        raise ValueError("settings must be an object")

    existing, _secret_set = current_settings(cp)
    merged = dict(existing)
    # current_settings() masks secrets for browser display. Server-side merge
    # must retain the actual stored value unless the caller explicitly changes it.
    for secret_key, secret_spec in SETTINGS_SCHEMA.items():
        if secret_spec["type"] == "secret":
            merged[secret_key] = cp.get(
                "config", secret_key, fallback=""
            )
    errors = {}

    for key, raw in updates.items():
        if key not in SETTINGS_SCHEMA:
            errors[key] = "unknown setting"
            continue

        if SETTINGS_SCHEMA[key]["type"] == "secret":
            # Blank secret means preserve current stored value.
            if raw is None or str(raw) == "":
                raw = cp.get("config", key, fallback="")
        try:
            merged[key] = coerce_setting(key, raw)
        except Exception as exc:
            errors[key] = str(exc)

    # Required fields.
    for key, spec in SETTINGS_SCHEMA.items():
        if not spec.get("required"):
            continue
        value = merged.get(key)
        if value is None or value == "" or value == []:
            errors.setdefault(key, "required")

    # Cross-field selection rules. vm_names_skip is a selection mode meaning
    # "all except", matching backup_native.py behavior.
    modes = []
    if merged.get("all_vms"):
        modes.append("all_vms")
    if merged.get("vm_tag"):
        modes.append("vm_tag")
    if merged.get("vm_names_skip"):
        modes.append("vm_names_skip")
    if merged.get("vm_names"):
        modes.append("vm_names")
    if len(modes) != 1:
        errors["vm_selection"] = (
            "select exactly one mode: vm_names, vm_names_skip, vm_tag, or all_vms"
        )

    server = str(merged.get("server") or "")
    parsed_server = urlparse(server)
    if server and (
        parsed_server.scheme not in ("http", "https")
        or not parsed_server.netloc
    ):
        errors["server"] = "must be an absolute http(s) URL"

    for key in (
        "backup_path",
        "native_logger_file_path",
        "native_restore_logger_file_path",
    ):
        value = str(merged.get(key) or "")
        if value and not os.path.isabs(value):
            errors[key] = "must be an absolute path"

    if merged.get("verify_tls"):
        ca_file = str(merged.get("ca_file") or "")
        if not ca_file:
            errors["ca_file"] = "required when TLS verification is enabled"
        elif not os.path.isabs(ca_file):
            errors["ca_file"] = "must be an absolute path"

    if errors:
        raise SettingsValidationError(errors)
    return merged


class SettingsValidationError(ValueError):
    def __init__(self, errors):
        super().__init__("settings validation failed")
        self.errors = errors


def config_schema_payload():
    result = []
    for key, spec in SETTINGS_SCHEMA.items():
        item = {"key": key}
        item.update(spec)
        result.append(item)
    return result


def settings_payload(cp, config_path):
    values, secret_set = current_settings(cp)
    return {
        "config_path": str(config_path),
        "schema": config_schema_payload(),
        "values": values,
        "secret_set": secret_set,
    }


def render_config_update(original, merged):
    lines = original.splitlines(keepends=True)
    output = []
    in_config = False
    found_config = False
    seen = set()

    key_pattern = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*?)(\r?\n)?$")
    section_pattern = re.compile(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?(?:\r?\n)?$")

    def append_missing():
        missing = [key for key in SETTINGS_SCHEMA if key not in seen]
        if missing:
            if output and output[-1].strip():
                output.append("\n")
            output.append("# Managed settings added by oVirt Backup Console.\n")
            for key in missing:
                output.append("%s=%s\n" % (key, serialize_setting(key, merged[key])))
                seen.add(key)

    for line in lines:
        section = section_pattern.match(line)
        if section:
            if in_config:
                append_missing()
            in_config = section.group(1).strip().lower() == "config"
            if in_config:
                found_config = True
            output.append(line)
            continue

        if in_config:
            match = key_pattern.match(line)
            if match:
                key = match.group(2).lower()
                if key in SETTINGS_SCHEMA:
                    newline = match.group(5) or "\n"
                    output.append(
                        "%s%s%s%s%s"
                        % (
                            match.group(1),
                            match.group(2),
                            match.group(3),
                            serialize_setting(key, merged[key]),
                            newline,
                        )
                    )
                    seen.add(key)
                    continue
        output.append(line)

    if in_config:
        append_missing()

    if not found_config:
        if output and output[-1].strip():
            output.append("\n")
        output.append("[config]\n")
        for key in SETTINGS_SCHEMA:
            output.append("%s=%s\n" % (key, serialize_setting(key, merged[key])))

    return "".join(output)


def atomic_save_settings(config_path, merged, backup_dir):
    path = Path(config_path)
    parent = path.parent
    if not parent.is_dir():
        raise RuntimeError("Config directory does not exist: %s" % parent)
    if not os.access(str(parent), os.W_OK):
        raise RuntimeError("Config directory is not writable: %s" % parent)

    original = path.read_text(encoding="utf-8") if path.exists() else "[config]\n"
    rendered = render_config_update(original, merged)

    # Parse the exact generated text before touching the live configuration.
    probe = configparser.RawConfigParser()
    probe.read_string(rendered)
    merge_settings(probe, {})

    backup_root = Path(backup_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = backup_root / ("%s.%s.bak" % (path.name, stamp))
        shutil.copy2(str(path), str(backup))
        os.chmod(str(backup), 0o640)
    else:
        backup = None

    tmp = parent / (".%s.tmp-%d-%d" % (path.name, os.getpid(), time.time_ns()))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(rendered)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(str(tmp), 0o640)
            os.replace(str(tmp), str(path))
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp.exists():
                tmp.unlink()
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    return str(backup) if backup else None


def operation_lock_state(path=DEFAULT_OPERATION_LOCK):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fh.seek(0)
        holder = fh.read().strip()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"busy": True, "holder": holder or "native operation"}
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return {"busy": False, "holder": holder}
    finally:
        fh.close()


class SchedulerControllerError(RuntimeError):
    def __init__(self, message, unavailable=False):
        super().__init__(message)
        self.unavailable = unavailable


def scheduler_rpc(socket_path, action, config=None, timeout=5):
    request = {"action": action}
    if config is not None:
        request["config"] = config

    payload = (
        json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(socket_path))
        try:
            client.sendall(payload)
            chunks = []
            total = 0
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 1024 * 1024:
                    raise SchedulerControllerError(
                        "scheduler controller response is too large"
                    )
                if b"\n" in chunk:
                    break
        finally:
            client.close()
    except SchedulerControllerError:
        raise
    except OSError as exc:
        raise SchedulerControllerError(
            "scheduler controller unavailable at %s: %s"
            % (socket_path, exc),
            unavailable=True,
        ) from exc

    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise SchedulerControllerError(
            "scheduler controller returned an empty response"
        )
    try:
        response = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SchedulerControllerError(
            "invalid scheduler controller response: %s" % exc
        ) from exc

    if not response.get("ok"):
        raise SchedulerControllerError(
            response.get("error") or "scheduler controller rejected request"
        )
    return response.get("result") or {}


def run_command(args, timeout=5):
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": result.stdout.strip(),
        }
    except Exception as exc:
        return {"ok": False, "returncode": None, "output": str(exc)}


def read_config(path):
    cp = configparser.RawConfigParser()
    with open(path, "r", encoding="utf-8") as fh:
        cp.read_file(fh)
    if not cp.has_section("config"):
        raise RuntimeError("Missing [config] section in %s" % path)
    return cp


def console_build_info():
    repo_root = Path(__file__).resolve().parent.parent
    git_dir = repo_root / ".git"
    commit = None
    branch = None

    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            branch = ref.rsplit("/", 1)[-1]
            ref_path = git_dir / ref
            if ref_path.is_file():
                commit = ref_path.read_text(encoding="utf-8").strip()
            else:
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(
                            encoding="utf-8"
                    ).splitlines():
                        if not line or line.startswith(("#", "^")):
                            continue
                        sha, name = line.split(" ", 1)
                        if name.strip() == ref:
                            commit = sha.strip()
                            break
        elif head:
            commit = head
    except Exception:
        commit = None
        branch = None

    return {
        "version": CONSOLE_VERSION,
        "commit": commit[:8] if commit else None,
        "branch": branch,
    }


def safe_settings(cp):
    result = {}
    for key, value in cp.items("config"):
        if any(word in key.lower() for word in SENSITIVE_KEYS):
            result[key] = "********" if value else ""
        else:
            result[key] = value
    return result


def get_backup_path(cp):
    value = cp.get("config", "backup_path", fallback="")
    return Path(value).expanduser() if value else None


def repository_stats(cp, probe_write=False):
    path = get_backup_path(cp)
    if path is None:
        return {
            "configured": False,
            "path": None,
            "exists": False,
            "mountpoint": False,
            "writable": False,
        }

    result = {
        "configured": True,
        "path": str(path),
        "exists": path.is_dir(),
        "mountpoint": os.path.ismount(str(path)),
        "writable": os.access(str(path), os.W_OK) if path.exists() else False,
    }
    if path.exists():
        st = os.statvfs(str(path))
        result.update({
            "total_bytes": st.f_blocks * st.f_frsize,
            "free_bytes": st.f_bavail * st.f_frsize,
            "used_bytes": (st.f_blocks - st.f_bfree) * st.f_frsize,
        })

    if probe_write and path.is_dir():
        probe = path / (
            ".ovirt-backup-console-write-test-%d-%d"
            % (os.getpid(), time.time_ns())
        )
        try:
            with open(probe, "wb") as fh:
                fh.write(b"ok\n")
                fh.flush()
                os.fsync(fh.fileno())
            result["writable"] = True
            result["write_probe"] = "ok"
        except Exception as exc:
            result["writable"] = False
            result["write_probe"] = str(exc)
        finally:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                result.setdefault("write_probe_cleanup_error", str(exc))
    return result


def systemd_state(unit):
    props = [
        "LoadState",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "ActiveEnterTimestamp",
        "InactiveEnterTimestamp",
        "NextElapseUSecRealtime",
        "LastTriggerUSec",
    ]
    cmd = run_command(
        ["systemctl", "show", unit, "--no-pager", "--property=" + ",".join(props)]
    )
    data = {"unit": unit, "available": cmd["ok"]}
    if cmd["ok"]:
        for line in cmd["output"].splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value
    else:
        data["error"] = cmd["output"]
    return data


def rpm_version(package):
    result = run_command(["rpm", "-q", package])
    return result["output"] if result["ok"] else None


ENGINE_RETRY_ERRORS = (sdk.Error, pycurl.error, ConnectionError, TimeoutError)


def engine_connection_options(cp):
    verify_tls = cp.getboolean("config", "verify_tls", fallback=True)
    ca_file = cp.get(
        "config", "ca_file", fallback="/etc/pki/ovirt-engine/ca.pem"
    )
    return {
        "url": cp.get("config", "server"),
        "username": cp.get("config", "username"),
        "password": cp.get("config", "password"),
        "ca_file": ca_file if verify_tls else None,
        "insecure": not verify_tls,
    }


class EngineSession:
    def __init__(self):
        self._lock = threading.Lock()
        self.stopping = False
        self._connection = None
        self._options = None

    def ensure_running(self):
        if self.stopping:
            raise RuntimeError("web console is stopping")

    @staticmethod
    def _close_connection(connection):
        if connection is None:
            return
        try:
            connection.close()
        except ENGINE_RETRY_ERRORS:
            # An unreachable Engine may prevent logout. Release local handles.
            LOG.warning("Engine session logout failed")
            connection.close(logout=False)

    def _discard(self):
        connection = self._connection
        self._connection = None
        self._options = None
        self._close_connection(connection)

    def close(self):
        self.stopping = True
        with self._lock:
            self._discard()

    def run(self, cp, operation, *, temporary=False):
        options = engine_connection_options(cp)
        with self._lock:
            self.ensure_running()
            if not temporary and self._options != options:
                self._discard()

            for attempt in range(2):
                self.ensure_running()
                connection = None if temporary else self._connection
                try:
                    if connection is None:
                        connection = sdk.Connection(**options, timeout=15)
                        if not temporary:
                            self._connection = connection
                            self._options = options
                    return operation(connection)
                except ENGINE_RETRY_ERRORS:
                    if not temporary:
                        self._discard()
                    if attempt:
                        raise
                finally:
                    if temporary:
                        self._close_connection(connection)


def engine_summary(cp, engine):
    started = time.monotonic()

    def query(connection):
        api = connection.system_service().get()
        version = None
        try:
            v = api.product_info.version
            version = ".".join(
                str(x)
                for x in (v.major, v.minor, v.build, v.revision)
                if x is not None
            )
        except Exception:
            pass
        return {
            "ok": True,
            "api_version": version,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }

    try:
        return engine.run(cp, query)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }


def config_from_merged(cp, merged):
    result = configparser.RawConfigParser()
    result.add_section("config")
    for key, value in cp.items("config"):
        result.set("config", key, value)
    for key, value in merged.items():
        result.set("config", key, serialize_setting(key, value))
    return result


def storage_domain_catalog(system):
    result = {}
    for storage in system.storage_domains_service().list():
        address = getattr(getattr(storage, "storage", None), "address", None)
        result[storage.id] = {
            "id": storage.id,
            "name": getattr(storage, "name", None),
            "status": str(getattr(storage, "status", None))
            if getattr(storage, "status", None) is not None else None,
            "type": str(getattr(storage, "type", None))
            if getattr(storage, "type", None) is not None else None,
            "address": address,
            "available": getattr(storage, "available", None),
        }
    return result


def vm_disk_details(connection, system, vm, engine, catalog):
    service = system.vms_service().vm_service(vm.id)
    items = []
    api_path = urlparse(connection.url).path.rstrip("/") + "/"
    for attachment in service.disk_attachments_service().list():
        engine.ensure_running()
        disk_id = attachment.disk.id
        href = attachment.disk.href
        if href and href.startswith(api_path):
            try:
                disk = connection.follow_link(attachment.disk)
            except sdk.NotFoundError:
                disk = system.disks_service().disk_service(disk_id).get()
        else:
            disk = system.disks_service().disk_service(disk_id).get()

        storage_ids = []
        for ref in getattr(disk, "storage_domains", None) or []:
            if getattr(ref, "id", None) and ref.id not in storage_ids:
                storage_ids.append(ref.id)
        singular = getattr(getattr(disk, "storage_domain", None), "id", None)
        if singular and singular not in storage_ids:
            storage_ids.append(singular)

        storages = []
        for storage_id in storage_ids:
            data = catalog.get(storage_id)
            if data is None:
                data = {
                    "id": storage_id,
                    "name": storage_id,
                    "status": None,
                    "type": None,
                    "address": None,
                    "available": None,
                }
            storages.append(data)

        items.append({
            "id": disk_id,
            "alias": getattr(disk, "alias", None) or getattr(disk, "name", None),
            "provisioned_size": getattr(disk, "provisioned_size", None),
            "actual_size": getattr(disk, "actual_size", None),
            "format": str(getattr(disk, "format", None))
            if getattr(disk, "format", None) is not None else None,
            "sparse": getattr(disk, "sparse", None),
            "bootable": bool(getattr(attachment, "bootable", False)),
            "active": bool(getattr(attachment, "active", False)),
            "interface": str(getattr(attachment, "interface", None))
            if getattr(attachment, "interface", None) is not None else None,
            "storage_domains": storages,
        })
    return items


def list_vms(cp, engine, details=True):
    def query(connection):
        system = connection.system_service()
        service = system.vms_service()
        catalog = storage_domain_catalog(system) if details else None
        all_vms = service.list(max=MAX_VMS)
        selected, _missing = resolve_selected_vms(system, cp, all_vms=all_vms)
        selected = set(selected)
        items = []
        for vm in all_vms:
            engine.ensure_running()
            cores = None
            if vm.cpu and vm.cpu.topology:
                cores = (
                    (vm.cpu.topology.cores or 1)
                    * (vm.cpu.topology.sockets or 1)
                    * (vm.cpu.topology.threads or 1)
                )
            item = {
                "id": vm.id,
                "name": vm.name,
                "configured_for_backup": vm.name in selected,
                "status": str(vm.status) if vm.status is not None else None,
                "memory": vm.memory,
                "cpu_cores": cores,
            }
            if details:
                try:
                    item["disks"] = vm_disk_details(
                        connection, system, vm, engine, catalog
                    )
                except Exception as exc:
                    if isinstance(exc, ENGINE_RETRY_ERRORS) and not isinstance(
                        exc, sdk.NotFoundError
                    ):
                        raise
                    item["disks"] = []
                    item["disk_error"] = str(exc)
            items.append(item)
        return sorted(items, key=lambda item: item["name"].lower())
    return engine.run(cp, query)


def resolve_selected_vms(system, cp, *, all_vms=None):
    vms_service = system.vms_service()
    if all_vms is None:
        all_vms = vms_service.list(max=MAX_VMS)
    by_name = {vm.name: vm for vm in all_vms}

    if cp.getboolean("config", "all_vms", fallback=False):
        selected = sorted(by_name)
    else:
        tag = cp.get("config", "vm_tag", fallback="").strip().strip('"')
        if tag:
            selected = sorted(
                vm.name
                for vm in vms_service.list(max=MAX_VMS, search="tag=%s" % tag)
            )
        else:
            skip = set(parse_list(cp.get("config", "vm_names_skip", fallback="[]")))
            if skip:
                selected = sorted(name for name in by_name if name not in skip)
            else:
                selected = parse_list(cp.get("config", "vm_names", fallback="[]"))

    missing = [name for name in selected if name not in by_name]
    return selected, missing


def config_checks(cp, engine, *, temporary=False):
    checks = []

    def check(name, ok, detail):
        return {"name": name, "ok": bool(ok), "detail": str(detail)}

    def add(name, ok, detail):
        checks.append(check(name, ok, detail))

    verify_tls = cp.getboolean("config", "verify_tls", fallback=True)
    ca_file = cp.get(
        "config", "ca_file", fallback="/etc/pki/ovirt-engine/ca.pem"
    )
    if verify_tls:
        ca_ok = os.path.isfile(ca_file) and os.access(ca_file, os.R_OK)
        add("TLS CA", ca_ok, ca_file if ca_ok else "not readable: %s" % ca_file)
    else:
        add("TLS CA", True, "TLS verification disabled")

    repo = repository_stats(cp, probe_write=True)
    add(
        "Backup repository",
        repo.get("exists", False),
        repo.get("path") or "not configured",
    )
    must_mount = cp.getboolean(
        "config", "backup_path_must_be_mount", fallback=True
    )
    add(
        "Repository mount",
        (not must_mount) or repo.get("mountpoint", False),
        (
            "mounted"
            if repo.get("mountpoint")
            else ("mount check disabled" if not must_mount else "not a mount point")
        ),
    )
    add(
        "Repository writable",
        repo.get("writable", False),
        "writable" if repo.get("writable") else "not writable",
    )
    if repo.get("free_bytes") is not None:
        add(
            "Repository free space",
            repo["free_bytes"] > 0,
            "%.2f GiB free" % (repo["free_bytes"] / 1024**3),
        )

    try:
        def query(connection):
            engine_checks = []

            def add(name, ok, detail):
                engine_checks.append(check(name, ok, detail))

            engine_data = {
                "ok": False,
                "selection": [],
                "storage_domains": [],
                "networks": [],
            }
            system = connection.system_service()
            api = system.get()
            version = None
            try:
                v = api.product_info.version
                version = ".".join(
                    str(x)
                    for x in (v.major, v.minor, v.build, v.revision)
                    if x is not None
                )
            except Exception:
                pass
            add("Engine API", True, "API %s" % (version or "unknown"))

            engine.ensure_running()
            selected, missing = resolve_selected_vms(system, cp)
            engine_data["selection"] = selected
            add(
                "VM selection",
                bool(selected) and not missing,
                (
                    "%d VM(s): %s" % (len(selected), ", ".join(selected))
                    if selected and not missing
                    else (
                        "missing VM(s): %s" % ", ".join(missing)
                        if missing else "no VMs selected"
                    )
                ),
            )

            engine.ensure_running()
            networks = [
                {
                    "id": network.id,
                    "name": getattr(network, "name", None),
                }
                for network in system.networks_service().list()
            ]
            engine_data["networks"] = sorted(
                networks, key=lambda item: (item["name"] or "").lower()
            )
            network_name = cp.get(
                "config", "imageio_network", fallback=""
            ).strip()
            if network_name:
                matches = [
                    item for item in networks if item["name"] == network_name
                ]
                add(
                    "ImageIO network",
                    len(matches) == 1,
                    (
                        network_name
                        if len(matches) == 1
                        else "%r: found %d matches" % (network_name, len(matches))
                    ),
                )
            else:
                add(
                    "ImageIO network",
                    True,
                    "not configured; Engine/default route will be used",
                )

            engine.ensure_running()
            catalog = storage_domain_catalog(system)
            engine_data["storage_domains"] = sorted(
                catalog.values(),
                key=lambda item: (item["name"] or "").lower(),
            )
            storage_name = cp.get(
                "config", "storage_domain", fallback=""
            ).strip()
            if storage_name:
                matches = [
                    item for item in catalog.values()
                    if item["name"] == storage_name
                ]
                add(
                    "Restore storage domain",
                    len(matches) == 1,
                    (
                        "%s (%s)" % (
                            storage_name,
                            matches[0].get("address") or "no address",
                        )
                        if len(matches) == 1
                        else "%r: found %d matches" % (
                            storage_name, len(matches)
                        )
                    ),
                )
            else:
                add(
                    "Restore storage domain",
                    True,
                    "optional for restore; scheduled backups use the backup repository",
                )

            cluster_name = cp.get(
                "config", "cluster_name", fallback="Default"
            ).strip()
            engine.ensure_running()
            clusters = system.clusters_service().list(
                search="name=%s" % cluster_name
            )
            exact_clusters = [
                cluster for cluster in clusters
                if getattr(cluster, "name", None) == cluster_name
            ]
            add(
                "Restore cluster",
                len(exact_clusters) == 1,
                "%s (%d match)" % (cluster_name, len(exact_clusters)),
            )
            engine_data["ok"] = True

            return engine_checks, engine_data

        engine_checks, engine_data = engine.run(cp, query, temporary=temporary)
        checks.extend(engine_checks)
    except Exception as exc:
        add("Engine API", False, str(exc))
        engine_data = {
            "ok": False,
            "selection": [],
            "storage_domains": [],
            "networks": [],
        }

    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "engine": engine_data,
        "repository": repo,
    }


def manifest_summary(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("backup manifest must be an object")
    except Exception as exc:
        return {
            "path": str(path.parent),
            "status": "invalid",
            "error": str(exc),
        }

    files = data.get("files") or []
    actual = sum((item.get("actual_size") or 0) for item in files)
    virtual = sum((item.get("virtual_size") or 0) for item in files)
    return {
        "path": str(path.parent),
        "directory": path.parent.name,
        "vm_name": data.get("vm_name"),
        "vm_id": data.get("vm_id"),
        "status": data.get("status"),
        "type": data.get("type"),
        "created_utc": data.get("created_utc"),
        "completed_utc": data.get("completed_utc"),
        "failed_utc": data.get("failed_utc"),
        "backup_id": data.get("backup_id"),
        "checkpoint_id": data.get("to_checkpoint_id"),
        "snapshot_id": data.get("snapshot_id"),
        "ovf_saved": data.get("ovf_saved"),
        "disk_count": len(files),
        "actual_size": actual,
        "virtual_size": virtual,
        "formats": sorted({
            str(item.get("format") or "").lower()
            for item in files
            if item.get("format")
        }),
        "duration_seconds": data.get("duration_seconds"),
    }


def retention_preview(cp):
    root = get_backup_path(cp)
    result = {
        "root": str(root) if root else None,
        "delete": [],
        "keep": [],
    }
    if root is None or not root.is_dir():
        return result

    days_raw = cp.get("config", "backup_keep_count", fallback="").strip()
    count_raw = cp.get(
        "config", "backup_keep_count_by_number", fallback=""
    ).strip()
    days = int(days_raw) if days_raw else None
    keep_count = int(count_raw) if count_raw else None
    now = datetime.now()

    for vm_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        completed = sorted(
            p for p in vm_dir.iterdir()
            if p.is_dir()
            and not p.name.endswith(".partial")
            and ".failed-" not in p.name
        )
        survivors = list(completed)
        reasons = {}

        if days is not None:
            cutoff = now.timestamp() - days * 86400
            for path in list(survivors):
                try:
                    stamp = datetime.strptime(
                        path.name, "%Y%m%d_%H%M%S"
                    ).timestamp()
                except ValueError:
                    continue
                if stamp < cutoff:
                    reasons.setdefault(path, []).append(
                        "age>%d days" % days
                    )
                    survivors.remove(path)

        if keep_count is not None:
            survivors.sort(key=lambda p: p.name)
            while len(survivors) > keep_count:
                path = survivors.pop(0)
                reasons.setdefault(path, []).append(
                    "count>%d" % keep_count
                )

        for path in completed:
            item = {
                "vm_name": vm_dir.name,
                "directory": path.name,
                "path": str(path),
            }
            if path in reasons:
                item["reasons"] = reasons[path]
                result["delete"].append(item)
            else:
                result["keep"].append(item)

    result["delete_count"] = len(result["delete"])
    result["keep_count"] = len(result["keep"])
    return result


BACKUP_DIRECTORY = r"\d{8}_\d{6}(?:\.partial|\.failed-\d{6})?"
DELETE_PENDING_SUFFIX = ".failed-delete"


def backup_deletable(path):
    name = path.name.removesuffix(DELETE_PENDING_SUFFIX)
    return bool(re.fullmatch(BACKUP_DIRECTORY, name)) and not (
        path.is_symlink() or path.parent.is_symlink()
    )


def ensure_no_nested_mounts(path):
    # mountinfo also reports bind mounts, including mounts on the same device.
    for entry in Path("/proc/self/mountinfo").read_text(
        encoding="utf-8", errors="surrogateescape"
    ).splitlines():
        mount = Path(re.sub(
            r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), entry.split()[4]
        ))
        if mount == path or path in mount.parents:
            raise ValueError("backup directory contains a mount point")


def delete_repository_backup(cp, value, audit):
    root = get_backup_path(cp)
    if root is None or not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("backup directory is required")
    root = root.absolute()
    target = Path(value).absolute()
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise ValueError("backup directory is outside configured repository") from None
    if (
        len(relative.parts) != 2
        or ".." in relative.parts
        or not backup_deletable(target)
    ):
        raise ValueError("invalid backup directory")
    if cp.getboolean("config", "backup_path_must_be_mount", fallback=True):
        if not root.resolve().is_mount():
            raise ValueError("backup repository is not mounted")
    expected = root.resolve() / relative
    if target.resolve() != expected or not expected.is_dir():
        raise ValueError("backup directory is missing or invalid")
    ensure_no_nested_mounts(expected)

    pending = expected
    if not expected.name.endswith(DELETE_PENDING_SUFFIX):
        manifest = expected / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("backup manifest is missing or invalid")
        data = read_json_file(manifest)
        if not isinstance(data, dict) or data.get("type") != "full":
            raise ValueError("only native full backups can be deleted here")
        pending = expected.with_name(expected.name + DELETE_PENDING_SUFFIX)
        if pending.exists() or pending.is_symlink():
            raise ValueError("a previous deletion is pending for this backup")
        try:
            expected.rename(pending)
        except PermissionError as exc:
            raise ValueError(
                "the web service user cannot remove this backup from the repository"
            ) from exc

    try:
        shutil.rmtree(pending)
    except OSError as exc:
        raise RuntimeError(
            "backup deletion incomplete; retry from the repository list: %s" % pending
        ) from exc
    LOG.info("Deleted repository backup: %s", expected)
    try:
        entry = "%s  DELETE  %s\n" % (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps(str(expected), ensure_ascii=False),
        )
        audit.write(entry.encode("utf-8", errors="backslashreplace"))
    except OSError:
        LOG.exception("Backup deleted but the console audit log could not be written")
        return {"ok": True, "audit_written": False}
    return {"ok": True, "audit_written": True}


def scan_backups(cp, vm_name=None, limit=250, *, include_pending=False):
    root = get_backup_path(cp)
    if root is None or not root.is_dir():
        return []

    if vm_name and (Path(vm_name).name != vm_name or vm_name in (".", "..")):
        return []
    scope = root / vm_name if vm_name else root
    pattern = "*" if vm_name else "*/*"
    entries = [
        (manifest.parent, False)
        for manifest in scope.glob(pattern + "/manifest.json")
        if not manifest.parent.name.endswith(DELETE_PENDING_SUFFIX)
        and not manifest.is_symlink()
        and not manifest.parent.is_symlink()
        and not manifest.parent.parent.is_symlink()
    ]
    if include_pending:
        entries.extend(
            (path, True) for path in scope.glob(pattern + DELETE_PENDING_SUFFIX)
            if path.is_dir() and backup_deletable(path)
        )

    entries.sort(key=lambda entry: entry[0].name, reverse=True)
    result = []
    for path, pending in entries[:limit]:
        if pending:
            item = {
                "path": str(path),
                "directory": path.name.removesuffix(DELETE_PENDING_SUFFIX),
                "vm_name": path.parent.name,
                "status": "delete_pending",
            }
        else:
            manifest = path / "manifest.json"
            item = manifest_summary(manifest)
            item.setdefault("directory", path.name)
            item.setdefault("vm_name", path.parent.name)
        item["can_delete"] = backup_deletable(path) and (
            pending or item.get("type") == "full"
        )
        result.append(item)
    return result


def tail_file(path, lines):
    if not path or not os.path.isfile(path):
        return []
    count = max(1, min(int(lines), 2000))
    result = run_command(["tail", "-n", str(count), path], timeout=5)
    if not result["ok"]:
        return ["ERROR: " + result["output"]]
    return result["output"].splitlines()


LOG_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:[.,](?:\d{6}|\d{3}))?(?:Z|[+-]\d{2}:\d{2})?"
)


def log_timestamp(line):
    match = LOG_TIMESTAMP.match(line)
    if match is None:
        return None, line
    try:
        stamp = datetime.fromisoformat(
            match.group().replace(",", ".").replace("Z", "+00:00")
        ).astimezone()
    except ValueError:
        return None, line
    return stamp, line[match.end():]


def merge_log_streams(streams, lines):
    entries = []
    for stream in streams:
        stamp = None
        entry = []
        for line in stream:
            next_stamp, message = log_timestamp(line)
            if next_stamp is not None:
                if entry:
                    entries.append((stamp, entry))
                stamp = next_stamp
                local_time = stamp.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                entry = [local_time + message]
            else:
                entry.append(line)
        if entry:
            entries.append((stamp, entry))
    entries.sort(key=lambda item: (item[0] is not None, item[0]))
    merged = [line for _stamp, entry in entries for line in entry]
    return merged[-max(1, min(lines, 2000)):]


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp-%d-%d" % (
        path.name, os.getpid(), time.time_ns()
    ))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True, default=str)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if tmp.exists():
                tmp.unlink()
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp-%d-%d" % (
        path.name, os.getpid(), time.time_ns()
    ))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
        finally:
            if tmp.exists():
                tmp.unlink()
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def validate_vm_name(value, field="target_name"):
    text = str(value or "").strip()
    if not text:
        raise ValueError("%s is required" % field)
    if len(text) > 255:
        raise ValueError("%s is too long" % field)
    if any(char in text for char in ("\n", "\r", "\x00")):
        raise ValueError("%s contains invalid control characters" % field)
    return text


def resolve_backup_directory(cp, value):
    root = get_backup_path(cp)
    if root is None:
        raise ValueError("backup_path is not configured")
    root = root.resolve()
    target = Path(str(value or "")).resolve()
    if root != target and root not in target.parents:
        raise ValueError("backup directory is outside configured repository")
    if target.name.endswith(DELETE_PENDING_SUFFIX):
        raise ValueError("backup deletion is pending")
    if not target.is_dir():
        raise ValueError("backup directory does not exist: %s" % target)
    manifest = target / "manifest.json"
    if not manifest.is_file():
        raise ValueError("manifest.json is missing: %s" % target)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("status") != "complete":
        raise ValueError(
            "backup is not complete: status=%r" % data.get("status")
        )
    return target, data


def restore_target_catalog(system, engine, *, include_eligible_storage=False):
    storages = sorted(
        storage_domain_catalog(system).values(),
        key=lambda item: (item.get("name") or "").lower(),
    )
    engine.ensure_running()
    clusters = []
    by_data_center = {}
    data_ids = {storage["id"] for storage in storages if storage["type"] == "data"}
    for cluster in system.clusters_service().list():
        item = {"id": cluster.id, "name": getattr(cluster, "name", None)}
        if include_eligible_storage:
            data_center = getattr(getattr(cluster, "data_center", None), "id", None)
            if data_center and data_center not in by_data_center:
                engine.ensure_running()
                service = system.data_centers_service().data_center_service(data_center)
                by_data_center[data_center] = [
                    domain.id for domain in service.storage_domains_service().list()
                    if domain.id in data_ids
                    and str(getattr(domain, "status", None)) == "active"
                ]
            item["storage_domain_ids"] = by_data_center.get(data_center, [])
        clusters.append(item)
    clusters.sort(key=lambda item: (item["name"] or "").lower())
    return {"storage_domains": storages, "clusters": clusters}


def settings_options(cp, engine):
    def query(connection):
        system = connection.system_service()
        targets = restore_target_catalog(system, engine)
        for key, list_resources in (
            ("networks", lambda: system.networks_service().list()),
            ("vms", lambda: system.vms_service().list()),
            ("tags", lambda: system.tags_service().list()),
            ("disks", lambda: system.disks_service().list()),
        ):
            engine.ensure_running()
            targets[key] = sorted(
                [
                    {
                        "id": resource.id,
                        "name": getattr(resource, "alias", None)
                        or getattr(resource, "name", None),
                    }
                    for resource in list_resources()
                ],
                key=lambda item: (item["name"] or "").lower(),
            )
        return targets

    return engine.run(cp, query)


def restore_options(cp, engine):
    def query(connection):
        targets = restore_target_catalog(
            connection.system_service(), engine, include_eligible_storage=True
        )
        return {
            **targets,
            "network_modes": [
                "none",
                "isolated",
                "isolated-original-mac",
                "original",
            ],
            "defaults": {
                "storage_domain": cp.get(
                    "config", "storage_domain", fallback=""
                ).strip(),
                "cluster": cp.get(
                    "config", "cluster_name", fallback="Default"
                ).strip(),
                "network_mode": "isolated",
            },
        }
    return engine.run(cp, query)


def legacy_backup_processes():
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
            "utf-8", errors="replace"
        ).strip()
        if not command:
            continue
        if "backup_native.py" in command:
            continue
        if re.search(r"(^|[ /])backup\.py(?: |$)", command):
            result.append({
                "pid": int(entry.name),
                "command": command,
            })
    return sorted(result, key=lambda item: item["pid"])


def health(cp, engine):
    result = config_checks(cp, engine)
    checks = list(result["checks"])

    for label, key, fallback in (
        (
            "Backup log",
            "native_logger_file_path",
            "/var/log/ovirt-backup/backup-native.log",
        ),
        (
            "Restore log",
            "native_restore_logger_file_path",
            "/var/log/ovirt-backup/restore-native.log",
        ),
    ):
        path = cp.get("config", key, fallback=fallback)
        checks.append({
            "name": label,
            "ok": bool(path) and os.path.exists(path),
            "detail": path or "not configured",
        })

    lock = operation_lock_state()
    checks.append({
        "name": "Native operation lock",
        "ok": True,
        "detail": (
            "busy: %s" % (lock.get("holder") or "native operation")
            if lock.get("busy") else "free"
        ),
    })
    return {
        "ok": all(item["ok"] for item in checks if item["name"] != "Native operation lock"),
        "checks": checks,
        "lock": lock,
    }


class BusyOperationError(RuntimeError):
    pass


class Application:
    def __init__(
            self,
            config_path,
            static_dir,
            token_file,
            backup_script,
            restore_script,
            runner_script,
            config_backup_dir,
            operation_dir,
            operation_lock,
            scheduler_socket):
        self.config_path = Path(config_path)
        self.static_dir = Path(static_dir)
        self.token_file = Path(token_file)
        self.backup_script = Path(backup_script)
        self.restore_script = Path(restore_script)
        self.runner_script = Path(runner_script)
        self.config_backup_dir = Path(config_backup_dir)
        self.operation_dir = Path(operation_dir)
        self.operation_lock = Path(operation_lock)
        self.scheduler_socket = Path(scheduler_socket)
        self.started = time.time()
        self.engine = EngineSession()
        self.config_lock = threading.Lock()
        self.scheduler_lock = threading.Lock()
        self.operation_start_lock = threading.Lock()

    def scheduler_status(self):
        return scheduler_rpc(self.scheduler_socket, "status")

    def scheduler_validate(self, config):
        return scheduler_rpc(
            self.scheduler_socket,
            "validate",
            config=config,
        )

    def scheduler_apply(self, config):
        requested_enabled = bool((config or {}).get("enabled"))
        if requested_enabled:
            cp = self.config()
            if cp.getboolean("config", "dry_run", fallback=False):
                raise SchedulerControllerError(
                    "native timer cannot be enabled while backup configuration has dry_run=True"
                )
            checks = self._append_config_storage_checks(
                config_checks(cp, self.engine)
            )
            failed = [
                item for item in checks["checks"]
                if not item.get("ok")
            ]
            if failed:
                raise SchedulerControllerError(
                    "native timer cannot be enabled because backup configuration "
                    "validation failed: %s"
                    % "; ".join(
                        "%s: %s" % (
                            item.get("name"),
                            item.get("detail"),
                        )
                        for item in failed
                    )
                )

        with self.scheduler_lock:
            previous = self.scheduler_status()["config"]
            result = scheduler_rpc(
                self.scheduler_socket,
                "apply",
                config=config,
                timeout=15,
            )
            changed = [
                key for key, value in result["config"].items()
                if previous.get(key) != value
            ]
            result["audit_written"] = self.audit_config_change("scheduler", changed)
            return result

    def scheduler_capability(self):
        try:
            result = self.scheduler_status()
            return {
                "write": True,
                "reason": None,
                "status": result,
            }
        except SchedulerControllerError as exc:
            return {
                "write": False,
                "reason": str(exc),
                "status": None,
            }

    def _ensure_no_legacy_backup(self):
        legacy = legacy_backup_processes()
        if legacy:
            raise BusyOperationError(
                "legacy backup.py is active: %s"
                % ", ".join(
                    "pid=%s" % item["pid"] for item in legacy
                )
            )

    def config(self):
        return read_config(self.config_path)

    def token(self):
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def bearer_authorized(self, headers):
        expected = self.token()
        auth = headers.get("Authorization", "")
        return bool(expected) and auth.startswith("Bearer ") and hmac.compare_digest(
            auth[7:].strip().encode("utf-8"), expected.encode("utf-8")
        )

    def session_cookie(self, token, remember):
        age = SESSION_MAX_AGE if remember else SESSION_BROWSER_AGE
        payload = "%d.%s" % (int(time.time()) + age, secrets.token_hex(16))
        signature = self.sign_session(token, payload)
        cookie = "%s=%s.%s; HttpOnly; Secure; SameSite=Strict" % (
            SESSION_COOKIE, payload, signature
        )
        if remember:
            cookie += "; Max-Age=%d" % age
        # The login URL is /api/session. The default cookie path covers /api
        # and also works when Apache mounts the console under a URL prefix.
        return cookie

    @staticmethod
    def sign_session(token, payload):
        return hmac.digest(
            token.encode("utf-8"), b"ovirt-backup-session-v1:" + payload.encode("ascii"),
            "sha256",
        ).hex()

    def authorized(self, headers):
        if self.bearer_authorized(headers):
            return True
        token = self.token()
        if not token:
            return False
        try:
            cookie = SimpleCookie(headers.get("Cookie", "")).get(SESSION_COOKIE)
        except CookieError:
            return False
        if cookie is None or not re.fullmatch(
            r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", cookie.value
        ):
            return False
        payload, signature = cookie.value.rsplit(".", 1)
        if int(payload.split(".", 1)[0]) <= time.time():
            return False
        return hmac.compare_digest(signature, self.sign_session(token, payload))

    def _append_config_storage_checks(self, result):
        config_parent = self.config_path.parent
        config_ok = (
            config_parent.is_dir()
            and os.access(str(config_parent), os.W_OK)
        )
        result["checks"].append({
            "name": "Config directory",
            "ok": config_ok,
            "detail": str(config_parent),
        })

        backup_parent = self.config_backup_dir
        backup_ok = (
            (backup_parent.is_dir() and os.access(str(backup_parent), os.W_OK))
            or (
                not backup_parent.exists()
                and backup_parent.parent.is_dir()
                and os.access(str(backup_parent.parent), os.W_OK)
            )
        )
        result["checks"].append({
            "name": "Config history directory",
            "ok": backup_ok,
            "detail": str(backup_parent),
        })
        result["ok"] = all(item["ok"] for item in result["checks"])
        return result

    def validate_settings(self, updates):
        cp = self.config()
        merged = merge_settings(cp, updates)
        draft = config_from_merged(cp, merged)
        temporary = engine_connection_options(draft) != engine_connection_options(cp)
        result = self._append_config_storage_checks(
            config_checks(draft, self.engine, temporary=temporary)
        )
        result["settings"] = {
            key: ("" if SETTINGS_SCHEMA[key]["type"] == "secret" else value)
            for key, value in merged.items()
        }
        return result

    def preview_retention(self, updates):
        cp = self.config()
        merged = merge_settings(cp, updates)
        draft = config_from_merged(cp, merged)
        return retention_preview(draft)

    def audit_config_change(self, kind, keys):
        if not keys:
            return True
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            self.operation_dir.mkdir(parents=True, exist_ok=True)
            with (self.operation_dir / "configuration.log").open(
                "a", encoding="utf-8"
            ) as audit:
                audit.write("%s  CONFIG  %s  %s\n" % (
                    stamp, kind, ", ".join(sorted(keys))
                ))
        except OSError:
            LOG.exception("Configuration changed but the audit log could not be written")
            return False
        return True

    def save_settings(self, updates):
        with self.config_lock:
            cp = self.config()
            merged = merge_settings(cp, updates)
            previous, _secret_set = current_settings(cp)
            previous["password"] = cp.get("config", "password", fallback="")
            changed = [key for key in merged if merged[key] != previous[key]]
            backup = atomic_save_settings(
                self.config_path,
                merged,
                self.config_backup_dir,
            )
            saved = self.config()
            audit_written = self.audit_config_change("backup settings", changed)

        return {
            "ok": True,
            "audit_written": audit_written,
            "backup": backup,
            "settings": settings_payload(saved, self.config_path),
            "checks": self._append_config_storage_checks(
                config_checks(saved, self.engine)
            ),
        }

    def current_operation(self, include_log=True):
        pointer = self.operation_dir / "current"
        try:
            operation_id = pointer.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {
                "active": False,
                "operation": None,
                "lock": operation_lock_state(self.operation_lock),
                "legacy_backup_processes": legacy_backup_processes(),
            }

        if not operation_id:
            return {
                "active": False,
                "operation": None,
                "lock": operation_lock_state(self.operation_lock),
                "legacy_backup_processes": legacy_backup_processes(),
            }

        state_path = self.operation_dir / ("%s.json" % operation_id)
        state = read_json_file(state_path)
        if state is None:
            return {
                "active": False,
                "operation": None,
                "error": "operation state is missing: %s" % state_path,
                "lock": operation_lock_state(self.operation_lock),
                "legacy_backup_processes": legacy_backup_processes(),
            }

        if include_log:
            state["log_tail"] = tail_file(state.get("log_path"), 160)

        return {
            "active": state.get("status") in ("starting", "running"),
            "operation": state,
            "lock": operation_lock_state(self.operation_lock),
            "legacy_backup_processes": legacy_backup_processes(),
        }

    def delete_backup(self, path):
        with self.operation_start_lock:
            if self.current_operation(include_log=False).get("active"):
                raise BusyOperationError("console operation is already active")
            self._ensure_no_legacy_backup()
            self.operation_lock.parent.mkdir(parents=True, exist_ok=True)
            with self.operation_lock.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise BusyOperationError("native operation is already active") from exc
                self.operation_dir.mkdir(parents=True, exist_ok=True)
                audit_path = self.operation_dir / "backup-deletions.log"
                with audit_path.open("ab", buffering=0) as audit:
                    return delete_repository_backup(self.config(), path, audit)

    def _start_operation(self, kind, command, metadata):
        with self.operation_start_lock:
            current = self.current_operation(include_log=False)
            if current.get("active"):
                op = current.get("operation") or {}
                raise BusyOperationError(
                    "console operation already active: %s %s"
                    % (op.get("kind"), op.get("id"))
                )

            lock = operation_lock_state(self.operation_lock)
            if lock.get("busy"):
                raise BusyOperationError(
                    "native operation is already active: %s"
                    % (lock.get("holder") or "unknown holder")
                )

            if not self.runner_script.is_file():
                raise RuntimeError(
                    "operation runner not found: %s" % self.runner_script
                )

            self.operation_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            operation_id = "%s-%s-%d" % (stamp, kind, time.time_ns())
            state_path = self.operation_dir / ("%s.json" % operation_id)
            log_path = self.operation_dir / ("%s.log" % operation_id)

            state = {
                "id": operation_id,
                "kind": kind,
                "status": "starting",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "state_path": str(state_path),
                "log_path": str(log_path),
                "command": command,
                "metadata": metadata or {},
            }
            atomic_write_json(state_path, state)
            atomic_write_text(
                self.operation_dir / "current",
                operation_id + "\n",
            )

            runner_command = [
                "/usr/bin/python3",
                str(self.runner_script),
                "--state",
                str(state_path),
                "--log",
                str(log_path),
                "--",
            ] + command

            try:
                subprocess.Popen(
                    runner_command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            except Exception as exc:
                state["status"] = "failed"
                state["completed_utc"] = datetime.now(
                    timezone.utc
                ).isoformat()
                state["runner_error"] = str(exc)
                atomic_write_json(state_path, state)
                raise

            return {
                "active": True,
                "operation": state,
                "lock": operation_lock_state(self.operation_lock),
                "legacy_backup_processes": legacy_backup_processes(),
            }

    def start_backup(self, vm_names):
        self._ensure_no_legacy_backup()
        cp = self.config()
        merge_settings(cp, {})
        if cp.getboolean("config", "dry_run", fallback=False):
            raise ValueError(
                "dry_run=True in configuration; disable it before Backup now"
            )

        names = parse_list(vm_names)
        if len(names) > MAX_VMS:
            raise ValueError("too many VM names")
        if not self.backup_script.is_file():
            raise RuntimeError(
                "backup_native.py not found: %s" % self.backup_script
            )

        command = [
            "/usr/bin/python3",
            str(self.backup_script),
            "-c",
            str(self.config_path),
        ]
        if names:
            command += [
                "--vm-names",
                json.dumps(names, ensure_ascii=False),
            ]

        return self._start_operation(
            "backup",
            command,
            {
                "vm_names": names,
                "selection": "explicit" if names else "configured",
            },
        )

    def _restore_command(self, request, execute):
        cp = self.config()
        backup_dir, manifest = resolve_backup_directory(
            cp, request.get("backup_dir")
        )
        source_name = manifest.get("vm_name") or "restored-vm"
        target_name = validate_vm_name(
            request.get("target_name")
            or (source_name + "_RESTORE_TEST")
        )

        storage = str(
            request.get("storage_domain")
            or cp.get("config", "storage_domain", fallback="")
        ).strip()
        if not storage:
            raise ValueError("storage_domain is required")

        cluster = str(
            request.get("cluster")
            or cp.get("config", "cluster_name", fallback="Default")
        ).strip()
        if not cluster:
            raise ValueError("cluster is required")

        network_mode = str(
            request.get("network_mode") or "isolated"
        ).strip().lower()
        if network_mode not in (
                "none",
                "isolated",
                "isolated-original-mac",
                "original",
        ):
            raise ValueError("invalid network_mode")

        if not self.restore_script.is_file():
            raise RuntimeError(
                "restore_native.py not found: %s" % self.restore_script
            )

        command = [
            "/usr/bin/python3",
            str(self.restore_script),
            "-c",
            str(self.config_path),
            "--backup-dir",
            str(backup_dir),
            "--target-name",
            target_name,
            "--cluster",
            cluster,
            "--storage-domain",
            storage,
            "--network-mode",
            network_mode,
        ]
        if execute:
            command.append("--execute")

        metadata = {
            "backup_dir": str(backup_dir),
            "source_vm_name": source_name,
            "target_name": target_name,
            "cluster": cluster,
            "storage_domain": storage,
            "network_mode": network_mode,
        }
        return command, metadata

    def restore_preflight(self, request):
        lock = operation_lock_state(self.operation_lock)
        if lock.get("busy"):
            raise BusyOperationError(
                "native operation is already active: %s"
                % (lock.get("holder") or "unknown holder")
            )
        command, metadata = self._restore_command(request, execute=False)
        result = run_command(command, timeout=600)
        return {
            "ok": result["ok"],
            "returncode": result["returncode"],
            "output": result["output"],
            "metadata": metadata,
        }

    def start_restore(self, request):
        self._ensure_no_legacy_backup()
        command, metadata = self._restore_command(request, execute=True)
        return self._start_operation("restore", command, metadata)

    def native_dry_run(self, vm_names):
        lock = operation_lock_state(self.operation_lock)
        if lock.get("busy"):
            raise BusyOperationError(
                "native operation is already active: %s"
                % (lock.get("holder") or "unknown holder")
            )

        cp = self.config()
        # Refuse to launch subprocess with a structurally invalid configuration.
        merge_settings(cp, {})

        names = parse_list(vm_names)
        if not names:
            raise SettingsValidationError({
                "vm_names": "select at least one VM for dry-run"
            })
        if len(names) > MAX_VMS:
            raise SettingsValidationError({
                "vm_names": "too many VM names"
            })

        if not self.backup_script.is_file():
            raise RuntimeError(
                "backup_native.py not found: %s" % self.backup_script
            )

        args = [
            "/usr/bin/python3",
            str(self.backup_script),
            "-c",
            str(self.config_path),
            "--dry-run",
            "--vm-names",
            json.dumps(names, ensure_ascii=False),
        ]
        result = run_command(args, timeout=300)
        return {
            "ok": result["ok"],
            "returncode": result["returncode"],
            "output": result["output"],
            "vm_names": names,
            "lock": operation_lock_state(self.operation_lock),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "oVirtBackupConsole/0.3"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'",
        )

    def send_json(self, data, status=HTTPStatus.OK, *, cookie=None):
        payload = json.dumps(
            data, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(payload)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self, max_bytes=1024 * 1024):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid JSON: %s" % exc)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def serve_file(self, path):
        root = self.app.static_dir.resolve()
        target = (root / path.lstrip("/")).resolve()
        if root != target and root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if not path.startswith("/api/"):
            self.serve_file("index.html" if path in ("/", "/index.html") else path)
            return

        if not self.app.authorized(self.headers):
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        if path == "/api/session":
            self.send_json({"ok": True})
            return

        try:
            cp = self.app.config()
            if path == "/api/overview":
                backups = scan_backups(cp, limit=50)
                self.send_json({
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "uptime_seconds": round(time.time() - self.app.started),
                    "engine": engine_summary(cp, self.app.engine),
                    "repository": repository_stats(cp),
                    "last_backup": backups[0] if backups else None,
                    "backup_count": len(backups),
                    "console": console_build_info(),
                    "service": systemd_state("ovirt-backup.service"),
                    "timer": systemd_state("ovirt-backup.timer"),
                    "lock": operation_lock_state(self.app.operation_lock),
                    "legacy_backup_processes": legacy_backup_processes(),
                    "packages": {
                        "ovirt_engine": rpm_version("ovirt-engine"),
                        "sdk": rpm_version("python3-ovirt-engine-sdk4"),
                        "imageio": rpm_version("ovirt-imageio-client"),
                    },
                })
            elif path == "/api/health":
                self.send_json(health(cp, self.app.engine))
            elif path == "/api/vms":
                details = query.get("details", ["1"])[0] not in ("0", "false", "False")
                self.send_json({"vms": list_vms(cp, self.app.engine, details=details)})
            elif path == "/api/backups":
                vm_name = query.get("vm", [None])[0]
                limit = int(query.get("limit", ["250"])[0])
                self.send_json({
                    "backups": scan_backups(cp, vm_name, limit, include_pending=True)
                })
            elif path == "/api/logs":
                lines = int(query.get("lines", ["400"])[0])
                kind = query.get("kind", ["backup"])[0]
                backup_log = cp.get(
                    "config", "native_logger_file_path",
                    fallback=cp.get("config", "logger_file_path", fallback=""),
                )
                restore_log = cp.get(
                    "config", "native_restore_logger_file_path",
                    fallback="/var/log/ovirt-backup/restore-native.log",
                )
                operations = self.app.operation_dir
                paths = {
                    "backup": [backup_log, operations / "backup-deletions.log"],
                    "restore": [restore_log],
                    "config": [operations / "configuration.log"],
                }
                if kind == "all":
                    sources = paths["backup"] + paths["restore"] + paths["config"]
                elif kind in paths:
                    sources = paths[kind]
                else:
                    raise ValueError("unknown log filter")
                streams = [tail_file(path, lines) for path in sources]
                self.send_json({
                    "kind": kind,
                    "path": str(sources[0]) if kind != "all" else None,
                    "lines": merge_log_streams(streams, lines),
                })
            elif path == "/api/settings":
                self.send_json(settings_payload(cp, self.app.config_path))
            elif path == "/api/settings/options":
                self.send_json(settings_options(cp, self.app.engine))
            elif path == "/api/operations/current":
                self.send_json(self.app.current_operation())
            elif path == "/api/restore-options":
                self.send_json(restore_options(cp, self.app.engine))
            elif path == "/api/scheduler":
                self.send_json(self.app.scheduler_status())
            elif path == "/api/capabilities":
                scheduler = self.app.scheduler_capability()
                self.send_json({
                    "mode": "configuration",
                    "actions_enabled": True,
                    "settings_write": True,
                    "dry_run": True,
                    "backup_now": True,
                    "restore": True,
                    "scheduler_write": scheduler["write"],
                    "reason": (
                        scheduler["reason"]
                        if not scheduler["write"]
                        else "Settings, manual operations and scheduler configuration are enabled."
                    ),
                })
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except SchedulerControllerError as exc:
            self.send_json(
                {"error": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE
                if exc.unavailable else HTTPStatus.CONFLICT,
            )
        except Exception as exc:
            LOG.exception("API request failed: %s", path)
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_session(self):
        if self.headers.get("X-oVirt-Console") != "1":
            self.send_json({"error": "missing console request header"}, HTTPStatus.FORBIDDEN)
            return
        try:
            body = self.read_json(max_bytes=4096)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if body.get("action") == "logout":
            self.send_json(
                {"ok": True},
                cookie=SESSION_COOKIE + "=; Max-Age=0; HttpOnly; Secure; SameSite=Strict",
            )
            return

        supplied = body.get("token")
        expected = self.app.token()
        if not expected or not isinstance(supplied, str) or not hmac.compare_digest(
            expected.encode("utf-8"), supplied.strip().encode("utf-8")
        ):
            self.send_json({"error": "invalid console token"}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_json(
            {"ok": True}, cookie=self.app.session_cookie(expected, body.get("remember") is True)
        )

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/session":
            self.handle_session()
            return

        if not self.app.authorized(self.headers):
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        if (self.headers.get("X-oVirt-Console") != "1"
                and not self.app.bearer_authorized(self.headers)):
            self.send_json({"error": "missing console request header"}, HTTPStatus.FORBIDDEN)
            return

        try:
            body = self.read_json()

            if path == "/api/settings/validate":
                result = self.app.validate_settings(
                    body.get("settings", {})
                )
                self.send_json(result)
                return

            if path == "/api/settings":
                result = self.app.save_settings(
                    body.get("settings", {})
                )
                self.send_json(result)
                return

            if path == "/api/settings/retention-preview":
                result = self.app.preview_retention(body.get("settings", {}))
                self.send_json(result)
                return

            if path == "/api/scheduler/validate":
                result = self.app.scheduler_validate(
                    body.get("scheduler", {})
                )
                self.send_json(result)
                return

            if path == "/api/scheduler":
                result = self.app.scheduler_apply(
                    body.get("scheduler", {})
                )
                self.send_json(result)
                return

            if path == "/api/backups/delete":
                if not self.app.token() or not self.app.authorized(self.headers):
                    self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                self.send_json(self.app.delete_backup(body.get("backup_dir")))
                return

            if path == "/api/actions/dry-run":
                result = self.app.native_dry_run(body.get("vm_names", []))
                status = HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_GATEWAY
                self.send_json(result, status)
                return

            if path == "/api/actions/backup":
                result = self.app.start_backup(body.get("vm_names", []))
                self.send_json(result, HTTPStatus.ACCEPTED)
                return

            if path == "/api/actions/restore-preflight":
                result = self.app.restore_preflight(body)
                status = HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_GATEWAY
                self.send_json(result, status)
                return

            if path == "/api/actions/restore":
                result = self.app.start_restore(body)
                self.send_json(result, HTTPStatus.ACCEPTED)
                return

            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except SettingsValidationError as exc:
            self.send_json(
                {
                    "error": "settings validation failed",
                    "errors": exc.errors,
                },
                HTTPStatus.BAD_REQUEST,
            )
        except SchedulerControllerError as exc:
            self.send_json(
                {"error": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE
                if exc.unavailable else HTTPStatus.CONFLICT,
            )
        except BusyOperationError as exc:
            self.send_json(
                {"error": str(exc)},
                HTTPStatus.CONFLICT,
            )
        except ValueError as exc:
            self.send_json(
                {"error": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            LOG.exception("POST request failed: %s", path)
            self.send_json(
                {"error": str(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def self_test():
    from unittest.mock import Mock, patch

    result = {"ok": False, "checks": []}

    def check(name, condition, detail=""):
        if not condition:
            raise AssertionError("%s: %s" % (name, detail))
        result["checks"].append({"name": name, "ok": True, "detail": detail})

    with tempfile.TemporaryDirectory(prefix="ovirt-backup-web-selftest-") as tmp:
        root = Path(tmp)
        config_dir = root / "config"
        config_dir.mkdir()
        config_path = config_dir / "backup.cfg"
        backup_dir = root / "config-backups"
        repo = root / "repository"
        repo.mkdir()

        config_path.write_text(
            "[config]\n"
            "vm_names=[\"demo\"]\n"
            "vm_names_skip=[]\n"
            "vm_tag=\n"
            "all_vms=False\n"
            "server=https://engine.example/ovirt-engine/api\n"
            "username=admin@internal\n"
            "password=s3cr%%t-value\n"
            "backup_path=%s\n"
            "backup_path_must_be_mount=False\n"
            "verify_tls=False\n"
            "imageio_timeout_policy=cancel\n"
            "dry_run=True\n"
            "logger_file_path=/var/log/legacy-native.log\n"
            "custom_flag: original\n"
            "custom_token=hidden\n"
            "custom_multiline=first\n"
            "  second\n"
            % repo,
            encoding="utf-8",
        )

        token_path = root / "web.token"
        token_path.write_text("self-test-token", encoding="utf-8")
        app = Application(
            config_path, root, token_path, root, root, root, backup_dir,
            root, root / "lock", root / "scheduler.sock",
        )
        cookie = app.session_cookie("self-test-token", remember=True)
        cookie_header = cookie.split(";", 1)[0]
        check(
            "persistent console sign-in",
            "Max-Age=2592000" in cookie
            and "HttpOnly; Secure; SameSite=Strict" in cookie
            and "self-test-token" not in cookie
            and app.authorized({"Cookie": cookie_header})
            and not app.authorized({"Cookie": cookie_header[:-1] + "!"}),
        )
        token_path.write_text("rotated-self-test-token", encoding="utf-8")
        check("token rotation invalidates sessions", not app.authorized({"Cookie": cookie_header}))

        cp = read_config(config_path)
        merged = merge_settings(cp, {
            "password": "",
            "imageio_max_workers": 8,
        })
        check(
            "secret preservation",
            merged["password"] == "s3cr%t-value",
            "blank browser secret preserved stored password",
        )
        check(
            "numeric coercion",
            merged["imageio_max_workers"] == 8,
            "integer setting validated",
        )

        backup = atomic_save_settings(config_path, merged, backup_dir)
        saved = read_config(config_path)
        check(
            "unmanaged config preserved",
            "unknown" not in settings_payload(saved, config_path)
            and saved.get("config", "native_logger_file_path") == "/var/log/legacy-native.log"
            and saved.get("config", "custom_flag") == "original"
            and saved.get("config", "custom_token") == "hidden"
            and saved.get("config", "custom_multiline") == "first\nsecond",
        )
        check(
            "atomic config save",
            saved.get("config", "password") == "s3cr%t-value"
            and saved.getint("config", "imageio_max_workers") == 8,
            "rendered config reparsed successfully",
        )
        check(
            "config history",
            bool(backup) and Path(backup).is_file(),
            str(backup),
        )

        engine = EngineSession()
        first, second, draft_connection, failed = (Mock() for _ in range(4))
        with patch.object(
            sdk, "Connection", side_effect=[first, second, draft_connection, failed]
        ) as connect:
            try:
                check(
                    "Engine session reuse",
                    engine.run(cp, lambda connection: connection) is first
                    and engine.run(saved, lambda connection: connection) is first
                    and connect.call_count == 1,
                )

                operation = Mock(side_effect=[sdk.Error("transport failed"), "ok"])
                check(
                    "Engine reconnect",
                    engine.run(cp, operation) == "ok"
                    and [call.args[0] for call in operation.call_args_list]
                    == [first, second]
                    and first.close.call_count == 1,
                )

                draft = config_from_merged(cp, {"server": "https://draft.example/api"})
                check(
                    "Draft Engine connection",
                    engine.run(draft, lambda connection: connection, temporary=True)
                    is draft_connection
                    and draft_connection.close.call_count == 1
                    and engine.run(cp, lambda connection: connection) is second,
                )

                operation = Mock(side_effect=sdk.Error("Engine unavailable"))
                try:
                    engine.run(cp, operation)
                except sdk.Error:
                    check(
                        "Engine retry limit",
                        operation.call_count == 2
                        and connect.call_count == 4
                        and second.close.call_count == 1
                        and failed.close.call_count == 1,
                    )
                else:
                    raise AssertionError("Engine failure was not propagated")
            finally:
                engine.close()

        with patch.object(sdk, "Connection", return_value=first):
            first.reset_mock()
            engine = EngineSession()
            engine.run(cp, lambda connection: None)
            engine.close()
            engine.close()
            check("Engine session shutdown", first.close.call_count == 1)

        vm_dir = repo / "demo"
        vm_dir.mkdir()
        for stamp in ("20261110_010000", "20261111_010000"):
            item = vm_dir / stamp
            item.mkdir()
            (item / "manifest.json").write_text(
                '{"status":"complete","type":"full"}\n',
                encoding="utf-8",
            )

        cp = read_config(config_path)
        merged = merge_settings(cp, {
            "backup_keep_count": "",
            "backup_keep_count_by_number": 1,
        })
        preview = retention_preview(config_from_merged(cp, merged))
        check(
            "retention preview",
            preview["delete_count"] == 1 and preview["keep_count"] == 1,
            "count policy previews without deleting",
        )
        check(
            "retention is read-only",
            all((vm_dir / stamp).is_dir() for stamp in (
                "20261110_010000", "20261111_010000"
            )),
            "preview left repository untouched",
        )

    log = merge_log_streams([
        ["2026-01-01T01:00:00+00:00  backup", "traceback continuation"],
        ["2026-01-01T02:30:00+01:00  CONFIG  scheduler  time"],
        ["2026-01-01T02:00:00+00:00  DELETE  copy"],
    ], 4)
    check(
        "combined log chronology",
        log[1] == "traceback continuation"
        and "CONFIG" in log[2] and "DELETE" in log[3],
    )
    result["ok"] = True
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="/etc/ovirt-backup/config/backup.cfg",
    )
    parser.add_argument("--static-dir", default="/usr/share/ovirt-backup/web")
    parser.add_argument("--token-file", default="/etc/ovirt-backup/web.token")
    parser.add_argument(
        "--backup-script",
        default="/usr/libexec/ovirt-backup/backup_native.py",
    )
    parser.add_argument(
        "--restore-script",
        default="/usr/libexec/ovirt-backup/restore_native.py",
    )
    parser.add_argument(
        "--runner-script",
        default="/usr/libexec/ovirt-backup/ovirt_backup_runner.py",
    )
    parser.add_argument(
        "--config-backup-dir",
        default=DEFAULT_CONFIG_BACKUP_DIR,
    )
    parser.add_argument(
        "--operation-dir",
        default="/var/lib/ovirt-backup/web-operations",
    )
    parser.add_argument(
        "--operation-lock",
        default=DEFAULT_OPERATION_LOCK,
    )
    parser.add_argument(
        "--scheduler-socket",
        default=DEFAULT_SCHEDULER_SOCKET,
    )
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local configuration, retention and Engine session self-test and exit.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.self_test:
        return self_test()

    app = Application(
        args.config,
        args.static_dir,
        args.token_file,
        args.backup_script,
        args.restore_script,
        args.runner_script,
        args.config_backup_dir,
        args.operation_dir,
        args.operation_lock,
        args.scheduler_socket,
    )
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.app = app
    LOG.info("oVirt Backup Console listening on http://%s:%s", args.listen, args.port)
    def stop_service(_signum, _frame):
        app.engine.stopping = True

    server.timeout = 0.5
    previous_sigterm = signal.signal(signal.SIGTERM, stop_service)
    try:
        while not app.engine.stopping:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            try:
                app.engine.close()
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
