#!/usr/bin/env python3
import argparse
import configparser
import hmac
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ovirtsdk4 as sdk

LOG = logging.getLogger("ovirt-backup-web")
MAX_VMS = 400

SENSITIVE_KEYS = {"password", "token", "secret", "web_token"}
DEFAULT_CONFIG_BACKUP_DIR = "/var/lib/ovirt-backup/config-backups"
DEFAULT_OPERATION_LOCK = "/var/lib/ovirt-backup/operation.lock"

# Ordered schema used by both validation and the browser settings form.
# Values are stored in the existing INI syntax expected by backup_native.py.
SETTINGS_SCHEMA = {
    "vm_names": {
        "group": "Virtual machines",
        "label": "Selected VMs",
        "type": "list",
        "default": [],
        "description": "Explicit VM names. Use one selection mode only.",
    },
    "vm_names_skip": {
        "group": "Virtual machines",
        "label": "Exclude VMs",
        "type": "list",
        "default": [],
        "description": "Back up every VM except these names.",
    },
    "vm_tag": {
        "group": "Virtual machines",
        "label": "VM tag",
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
        "type": "list",
        "default": [],
    },
    "cluster_name": {
        "group": "Restore defaults",
        "label": "Target cluster",
        "type": "string",
        "default": "Default",
    },
    "storage_domain": {
        "group": "Restore defaults",
        "label": "Target storage domain",
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
    known = set(SETTINGS_SCHEMA)
    unknown = {
        key: value
        for key, value in cp.items("config")
        if key not in known and not any(word in key.lower() for word in SENSITIVE_KEYS)
    }
    return {
        "config_path": str(config_path),
        "schema": config_schema_payload(),
        "values": values,
        "secret_set": secret_set,
        "unknown": unknown,
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


def repository_stats(cp):
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


def connect_engine(cp):
    verify_tls = cp.getboolean("config", "verify_tls", fallback=True)
    ca_file = cp.get(
        "config", "ca_file", fallback="/etc/pki/ovirt-engine/ca.pem"
    )
    return sdk.Connection(
        url=cp.get("config", "server"),
        username=cp.get("config", "username"),
        password=cp.get("config", "password"),
        ca_file=ca_file if verify_tls else None,
        insecure=not verify_tls,
        debug=False,
    )


def engine_summary(cp):
    started = time.monotonic()
    try:
        connection = connect_engine(cp)
        try:
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
        finally:
            connection.close()
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }


def list_vms(cp):
    connection = connect_engine(cp)
    try:
        service = connection.system_service().vms_service()
        items = []
        for vm in service.list(max=MAX_VMS):
            cores = None
            if vm.cpu and vm.cpu.topology:
                cores = (
                    (vm.cpu.topology.cores or 1)
                    * (vm.cpu.topology.sockets or 1)
                    * (vm.cpu.topology.threads or 1)
                )
            items.append({
                "id": vm.id,
                "name": vm.name,
                "status": str(vm.status) if vm.status is not None else None,
                "memory": vm.memory,
                "cpu_cores": cores,
            })
        return sorted(items, key=lambda item: item["name"].lower())
    finally:
        connection.close()


def manifest_summary(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    }


def scan_backups(cp, vm_name=None, limit=250):
    root = get_backup_path(cp)
    if root is None or not root.is_dir():
        return []

    if vm_name:
        vm_path = root / vm_name
        manifests = list(vm_path.glob("*/manifest.json")) if vm_path.is_dir() else []
    else:
        manifests = list(root.glob("*/*/manifest.json"))

    manifests.sort(key=lambda p: p.parent.name, reverse=True)
    return [manifest_summary(p) for p in manifests[:limit]]


def tail_file(path, lines):
    if not path or not os.path.isfile(path):
        return []
    count = max(1, min(int(lines), 2000))
    result = run_command(["tail", "-n", str(count), path], timeout=5)
    if not result["ok"]:
        return ["ERROR: " + result["output"]]
    return result["output"].splitlines()


def health(cp):
    repo = repository_stats(cp)
    engine = engine_summary(cp)
    native_log = cp.get(
        "config",
        "native_logger_file_path",
        fallback=cp.get("config", "logger_file_path", fallback=""),
    )
    checks = [
        {
            "name": "Engine API",
            "ok": engine.get("ok", False),
            "detail": (
                "API %s, %.1f ms"
                % (engine.get("api_version") or "unknown", engine.get("latency_ms", 0))
                if engine.get("ok")
                else engine.get("error")
            ),
        },
        {
            "name": "Backup repository",
            "ok": repo.get("exists", False),
            "detail": repo.get("path") or "not configured",
        },
        {
            "name": "Repository mount",
            "ok": repo.get("mountpoint", False),
            "detail": "mounted" if repo.get("mountpoint") else "not a mount point",
        },
        {
            "name": "Repository writable",
            "ok": repo.get("writable", False),
            "detail": "writable" if repo.get("writable") else "not writable",
        },
        {
            "name": "Native log",
            "ok": bool(native_log) and os.path.exists(native_log),
            "detail": native_log or "not configured",
        },
    ]
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


class Application:
    def __init__(self, config_path, static_dir, token_file):
        self.config_path = config_path
        self.static_dir = Path(static_dir)
        self.token_file = Path(token_file)
        self.started = time.time()

    def config(self):
        return read_config(self.config_path)

    def token(self):
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def authorized(self, headers):
        expected = self.token()
        if not expected:
            return True
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip(), expected)


class Handler(BaseHTTPRequestHandler):
    server_version = "oVirtBackupConsole/0.1"

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

    def send_json(self, data, status=HTTPStatus.OK):
        payload = json.dumps(
            data, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(payload)

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

        try:
            cp = self.app.config()
            if path == "/api/overview":
                backups = scan_backups(cp, limit=50)
                self.send_json({
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "uptime_seconds": round(time.time() - self.app.started),
                    "engine": engine_summary(cp),
                    "repository": repository_stats(cp),
                    "last_backup": backups[0] if backups else None,
                    "backup_count": len(backups),
                    "service": systemd_state("ovirt-backup.service"),
                    "timer": systemd_state("ovirt-backup.timer"),
                    "packages": {
                        "ovirt_engine": rpm_version("ovirt-engine"),
                        "sdk": rpm_version("python3-ovirt-engine-sdk4"),
                        "imageio": rpm_version("ovirt-imageio-client"),
                    },
                })
            elif path == "/api/health":
                self.send_json(health(cp))
            elif path == "/api/vms":
                self.send_json({"vms": list_vms(cp)})
            elif path == "/api/backups":
                vm_name = query.get("vm", [None])[0]
                limit = int(query.get("limit", ["250"])[0])
                self.send_json({"backups": scan_backups(cp, vm_name, limit)})
            elif path == "/api/logs":
                lines = int(query.get("lines", ["400"])[0])
                logfile = cp.get(
                    "config",
                    "native_logger_file_path",
                    fallback=cp.get("config", "logger_file_path", fallback=""),
                )
                self.send_json({
                    "path": logfile,
                    "lines": tail_file(logfile, lines),
                })
            elif path == "/api/settings":
                self.send_json({"settings": safe_settings(cp)})
            elif path == "/api/scheduler":
                self.send_json({
                    "service": systemd_state("ovirt-backup.service"),
                    "timer": systemd_state("ovirt-backup.timer"),
                })
            elif path == "/api/capabilities":
                self.send_json({
                    "mode": "read-only",
                    "actions_enabled": False,
                    "reason": "Native backup/restore validation is not complete yet.",
                })
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            LOG.exception("API request failed: %s", path)
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        if not self.app.authorized(self.headers):
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_json(
            {
                "error": "write actions are disabled in web console 0.1",
                "mode": "read-only",
            },
            HTTPStatus.NOT_IMPLEMENTED,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/ovirt-backup/backup.cfg")
    parser.add_argument("--static-dir", default="/usr/share/ovirt-backup/web")
    parser.add_argument("--token-file", default="/etc/ovirt-backup/web.token")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    app = Application(args.config, args.static_dir, args.token_file)
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.app = app
    LOG.info("oVirt Backup Console listening on http://%s:%s", args.listen, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
