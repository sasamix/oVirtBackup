#!/usr/bin/env python3
"""Persistent runner for manual oVirt Backup Console operations."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("command after -- is required")

    state_path = Path(args.state)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    state = load_state(state_path)
    state.update({
        "status": "running",
        "runner_pid": os.getpid(),
        "started_utc": utc_now(),
    })
    save_state(state_path, state)

    returncode = None
    child = None
    try:
        with open(log_path, "a", encoding="utf-8", buffering=1) as log:
            log.write(
                "%s runner_pid=%d START %s\n"
                % (utc_now(), os.getpid(), " ".join(command))
            )
            log.flush()
            child = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            state = load_state(state_path)
            state["child_pid"] = child.pid
            save_state(state_path, state)

            returncode = child.wait()
            log.write(
                "%s runner_pid=%d EXIT returncode=%d\n"
                % (utc_now(), os.getpid(), returncode)
            )
            log.flush()

        state = load_state(state_path)
        state.update({
            "status": "complete" if returncode == 0 else "failed",
            "returncode": returncode,
            "completed_utc": utc_now(),
        })
        save_state(state_path, state)
        return returncode

    except BaseException as exc:
        state = load_state(state_path)
        state.update({
            "status": "failed",
            "returncode": returncode,
            "completed_utc": utc_now(),
            "runner_error": "%s: %s" % (type(exc).__name__, exc),
        })
        try:
            save_state(state_path, state)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
