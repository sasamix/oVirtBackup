#!/usr/bin/env python3
import argparse
import configparser
import hmac
import json
import logging
import os
import subprocess
import sys
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
                    vm.cpu.topology.cores
                    * vm.cpu.topology.sockets
                    * vm.cpu.topology.threads
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
