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
import tempfile
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
CONSOLE_VERSION = "0.3.0"

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


def vm_disk_details(connection, system, vm, catalog=None):
    service = system.vms_service().vm_service(vm.id)
    if catalog is None:
        catalog = storage_domain_catalog(system)
    items = []
    for attachment in service.disk_attachments_service().list():
        disk_id = attachment.disk.id
        try:
            disk = connection.follow_link(attachment.disk)
        except Exception:
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


def list_vms(cp, details=True):
    connection = connect_engine(cp)
    try:
        system = connection.system_service()
        service = system.vms_service()
        catalog = storage_domain_catalog(system) if details else None
        items = []
        for vm in service.list(max=MAX_VMS):
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
                "status": str(vm.status) if vm.status is not None else None,
                "memory": vm.memory,
                "cpu_cores": cores,
            }
            if details:
                try:
                    item["disks"] = vm_disk_details(
                        connection, system, vm, catalog=catalog
                    )
                except Exception as exc:
                    item["disks"] = []
                    item["disk_error"] = str(exc)
            items.append(item)
        return sorted(items, key=lambda item: item["name"].lower())
    finally:
        connection.close()


def resolve_selected_vms(system, cp):
    vms_service = system.vms_service()
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


def config_checks(cp):
    checks = []

    def add(name, ok, detail, **extra):
        item = {"name": name, "ok": bool(ok), "detail": str(detail)}
        item.update(extra)
        checks.append(item)

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

    engine_data = {
        "ok": False,
        "selection": [],
        "storage_domains": [],
        "networks": [],
    }
    try:
        connection = connect_engine(cp)
        try:
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
                    "not configured; restore must choose one explicitly",
                )

            cluster_name = cp.get(
                "config", "cluster_name", fallback="Default"
            ).strip()
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
        finally:
            connection.close()
    except Exception as exc:
        add("Engine API", False, str(exc))

    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "engine": engine_data,
        "repository": repo,
    }


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


def restore_options(cp):
    connection = connect_engine(cp)
    try:
        system = connection.system_service()
        storages = sorted(
            storage_domain_catalog(system).values(),
            key=lambda item: (item.get("name") or "").lower(),
        )
        clusters = sorted(
            [
                {
                    "id": cluster.id,
                    "name": getattr(cluster, "name", None),
                }
                for cluster in system.clusters_service().list()
            ],
            key=lambda item: (item.get("name") or "").lower(),
        )
        return {
            "storage_domains": storages,
            "clusters": clusters,
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
    finally:
        connection.close()


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


def health(cp):
    result = config_checks(cp)
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
            operation_lock):
        self.config_path = Path(config_path)
        self.static_dir = Path(static_dir)
        self.token_file = Path(token_file)
        self.backup_script = Path(backup_script)
        self.restore_script = Path(restore_script)
        self.runner_script = Path(runner_script)
        self.config_backup_dir = Path(config_backup_dir)
        self.operation_dir = Path(operation_dir)
        self.operation_lock = Path(operation_lock)
        self.started = time.time()
        self.config_lock = threading.Lock()
        self.operation_start_lock = threading.Lock()

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

    def authorized(self, headers):
        expected = self.token()
        if not expected:
            return True
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip(), expected)

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
        result = self._append_config_storage_checks(config_checks(draft))
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

    def save_settings(self, updates):
        with self.config_lock:
            cp = self.config()
            merged = merge_settings(cp, updates)
            backup = atomic_save_settings(
                self.config_path,
                merged,
                self.config_backup_dir,
            )
            saved = self.config()

        return {
            "ok": True,
            "backup": backup,
            "settings": settings_payload(saved, self.config_path),
            "checks": self._append_config_storage_checks(
                config_checks(saved)
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
                self.send_json(health(cp))
            elif path == "/api/vms":
                details = query.get("details", ["1"])[0] not in ("0", "false", "False")
                self.send_json({"vms": list_vms(cp, details=details)})
            elif path == "/api/backups":
                vm_name = query.get("vm", [None])[0]
                limit = int(query.get("limit", ["250"])[0])
                self.send_json({"backups": scan_backups(cp, vm_name, limit)})
            elif path == "/api/logs":
                lines = int(query.get("lines", ["400"])[0])
                kind = query.get("kind", ["backup"])[0]
                if kind == "restore":
                    logfile = cp.get(
                        "config",
                        "native_restore_logger_file_path",
                        fallback="/var/log/ovirt-backup/restore-native.log",
                    )
                else:
                    kind = "backup"
                    logfile = cp.get(
                        "config",
                        "native_logger_file_path",
                        fallback="/var/log/ovirt-backup/backup-native.log",
                    )
                self.send_json({
                    "kind": kind,
                    "path": logfile,
                    "lines": tail_file(logfile, lines),
                })
            elif path == "/api/settings":
                self.send_json(settings_payload(cp, self.app.config_path))
            elif path == "/api/operations/current":
                self.send_json(self.app.current_operation())
            elif path == "/api/restore-options":
                self.send_json(restore_options(cp))
            elif path == "/api/scheduler":
                self.send_json({
                    "service": systemd_state("ovirt-backup.service"),
                    "timer": systemd_state("ovirt-backup.timer"),
                })
            elif path == "/api/capabilities":
                self.send_json({
                    "mode": "configuration",
                    "actions_enabled": True,
                    "settings_write": True,
                    "dry_run": True,
                    "backup_now": True,
                    "restore": True,
                    "scheduler_write": False,
                    "reason": (
                        "Settings, dry-run, manual backup and restore are enabled. "
                        "Scheduler write actions remain disabled."
                    ),
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

        path = urlparse(self.path).path
        try:
            body = self.read_json()

            if path == "/api/settings/validate":
                result = self.app.validate_settings(body.get("settings", {}))
                self.send_json(result)
                return

            if path == "/api/settings":
                result = self.app.save_settings(body.get("settings", {}))
                self.send_json(result)
                return

            if path == "/api/settings/retention-preview":
                result = self.app.preview_retention(body.get("settings", {}))
                self.send_json(result)
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
            % repo,
            encoding="utf-8",
        )

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
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local configuration/retention self-test and exit.",
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
    )
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
