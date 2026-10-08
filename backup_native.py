#!/usr/bin/env python3
"""
Native FULL backup for oVirt using VmBackupsService + ImageTransfer.

This script deliberately avoids the deprecated Export Domain workflow:
no manual snapshot, no temporary cloned VM, no VM export.
"""

import argparse
import configparser
import datetime as dt
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import ovirtsdk4 as sdk
import ovirtsdk4.types as types
from ovirt_imageio import client as imageio_client


LOG = logging.getLogger("ovirt-native-backup")
MAX_VMS = 400


def cfg_json(cp, name, default):
    raw = cp.get("config", name, fallback=None)
    return default if raw in (None, "") else json.loads(raw)


def load_config(path):
    cp = configparser.RawConfigParser()
    with open(path, "r", encoding="utf-8") as fh:
        cp.read_file(fh)
    if not cp.has_section("config"):
        raise RuntimeError("Missing [config] section")
    return cp


def configure_logging(cp, debug):
    fmt = cp.get("config", "logger_fmt", fallback="%(asctime)s: %(message)s")
    filename = cp.get(
        "config",
        "native_logger_file_path",
        fallback=cp.get("config", "logger_file_path", fallback=""),
    )
    opts = {
        "format": fmt,
        "level": logging.DEBUG if debug else logging.INFO,
    }
    if filename:
        opts["filename"] = filename
    logging.basicConfig(**opts)


def connect(cp):
    ca_file = cp.get(
        "config", "ca_file", fallback="/etc/pki/ovirt-engine/ca.pem"
    )
    verify_tls = cp.getboolean("config", "verify_tls", fallback=True)
    if verify_tls and not os.path.isfile(ca_file):
        raise RuntimeError("CA file does not exist: %s" % ca_file)

    return sdk.Connection(
        url=cp.get("config", "server"),
        username=cp.get("config", "username"),
        password=cp.get("config", "password"),
        ca_file=ca_file if verify_tls else None,
        insecure=not verify_tls,
        debug=False,
    )


def destination_preflight(cp):
    raw = cp.get("config", "backup_path", fallback="")
    if not raw:
        raise RuntimeError(
            "backup_path is required. Use a normal filesystem/NFS mount, "
            "not an oVirt Export Domain directory."
        )

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeError("backup_path must be absolute")
    if not path.is_dir():
        raise RuntimeError(
            "backup_path must already exist: %s. It is not created automatically "
            "to avoid writing locally when an NFS mount is missing." % path
        )

    must_mount = cp.getboolean(
        "config", "backup_path_must_be_mount", fallback=True
    )
    if must_mount and not os.path.ismount(str(path)):
        raise RuntimeError(
            "backup_path_must_be_mount=True but %s is not a mount point" % path
        )

    probe = path / (".ovirt-backup-write-test-%d" % os.getpid())
    try:
        with open(probe, "wb") as fh:
            fh.write(b"ok\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass

    return path


def select_vm_names(system, cp, override):
    if override is not None:
        return json.loads(override)

    vms = system.vms_service()

    if cp.getboolean("config", "all_vms", fallback=False):
        return [vm.name for vm in vms.list(max=MAX_VMS)]

    tag = cp.get("config", "vm_tag", fallback="").strip().strip('"')
    if tag:
        return [
            vm.name
            for vm in vms.list(max=MAX_VMS, search="tag=%s" % tag)
        ]

    skip = set(cfg_json(cp, "vm_names_skip", []))
    if skip:
        return [
            vm.name for vm in vms.list(max=MAX_VMS)
            if vm.name not in skip
        ]

    return cfg_json(cp, "vm_names", [])


def find_vm(vms_service, name):
    matches = vms_service.list(search="name=%s" % name)
    exact = [vm for vm in matches if vm.name == name]
    if not exact:
        raise RuntimeError("VM %r not found" % name)
    if len(exact) != 1:
        raise RuntimeError("VM name %r is not unique" % name)
    return exact[0]


def collect_nics(vm_service):
    result = []
    for nic in vm_service.nics_service().list():
        mac = getattr(getattr(nic, "mac", None), "address", None)
        profile = getattr(nic, "vnic_profile", None)
        result.append({
            "id": getattr(nic, "id", None),
            "name": getattr(nic, "name", None),
            "mac": mac,
            "interface": str(getattr(nic, "interface", None))
            if getattr(nic, "interface", None) is not None else None,
            "plugged": getattr(nic, "plugged", None),
            "linked": getattr(nic, "linked", None),
            "vnic_profile_id": getattr(profile, "id", None) if profile else None,
            "vnic_profile_name": getattr(profile, "name", None) if profile else None,
        })
    return result


def select_disks(system, vm_service, cp):
    bootable_only = cp.getboolean("config", "bootable_only", fallback=False)
    include_inactive = cp.getboolean(
        "config", "with_disks_deactivated", fallback=False
    )
    exclude = set(cfg_json(cp, "disks_id_exclude", []))

    selected = []
    for att in vm_service.disk_attachments_service().list():
        disk_id = att.disk.id

        if disk_id in exclude:
            LOG.info("Excluded disk: %s", disk_id)
            continue
        if bootable_only and not att.bootable:
            continue
        if not include_inactive and not att.active:
            LOG.info("Skipping deactivated disk: %s", disk_id)
            continue

        selected.append(att)
        if bootable_only:
            break

    if not selected:
        raise RuntimeError("No disks selected")

    inactive = [att.disk.id for att in selected if not att.active]
    if inactive:
        raise RuntimeError(
            "Native VM Backup API requires selected disks to be active/plugged: %s"
            % ", ".join(inactive)
        )

    api_disks = []
    metadata = []
    disks_service = system.disks_service()

    for att in selected:
        disk_id = att.disk.id
        disk = disks_service.disk_service(disk_id).get()
        api_disks.append(types.Disk(id=disk_id))
        metadata.append({
            "id": disk_id,
            "name": getattr(disk, "name", None),
            "alias": getattr(disk, "alias", None),
            "bootable": bool(att.bootable),
            "active": bool(att.active),
            "interface": str(att.interface) if att.interface else None,
            "provisioned_size": getattr(disk, "provisioned_size", None),
            "actual_size": getattr(disk, "actual_size", None),
            "format": str(disk.format) if getattr(disk, "format", None) else None,
            "sparse": getattr(disk, "sparse", None),
        })

    return api_disks, metadata


def free_bytes(path):
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


def check_destination_space(path, disks, cp):
    threshold = cp.getfloat(
        "config", "storage_space_threshold", fallback=0.1
    )
    provisioned = sum(d.get("provisioned_size") or 0 for d in disks)
    required = int(provisioned * (1.0 + max(0.0, threshold)))
    available = free_bytes(path)

    LOG.info(
        "Destination free: %.2f GiB; conservative required: %.2f GiB",
        available / 1024**3,
        required / 1024**3,
    )
    if required and available < required:
        raise RuntimeError(
            "Not enough destination space: need %.2f GiB, have %.2f GiB"
            % (required / 1024**3, available / 1024**3)
        )


def wait_backup(backup_service, backup, wanted, timeout):
    started = time.monotonic()
    last = None
    while backup.phase != wanted:
        if backup.phase == types.BackupPhase.FAILED:
            raise RuntimeError("Engine backup %s failed" % backup.id)
        if backup.phase != last:
            LOG.info("Backup %s phase: %s", backup.id, backup.phase)
            last = backup.phase
        if time.monotonic() - started > timeout:
            raise RuntimeError(
                "Timeout waiting for backup %s: phase=%s"
                % (backup.id, backup.phase)
            )
        time.sleep(1)
        backup = backup_service.get()
    return backup


def _extract_ovf(source):
    try:
        return source.initialization.configuration.data
    except (AttributeError, TypeError):
        return None


def save_ovf(vms_service, vm_service, vm_id, backup, target):
    snapshot_id = getattr(getattr(backup, "snapshot", None), "id", None)
    ovf = None

    if snapshot_id:
        try:
            source = (
                vm_service.snapshots_service()
                .snapshot_service(snapshot_id)
                .get(all_content=True)
            )
            ovf = _extract_ovf(source)
            if ovf:
                LOG.info("Using OVF from backup snapshot %s", snapshot_id)
            else:
                LOG.warning(
                    "Backup snapshot %s returned no OVF; "
                    "falling back to current VM OVF",
                    snapshot_id,
                )
        except Exception:
            LOG.exception(
                "Cannot read backup snapshot OVF; falling back to current VM OVF"
            )

    if not ovf:
        # Official oVirt SDK backup examples fetch native oVirt OVF from the
        # VMs collection with all_content=True. Do not use ovf_as_ova here:
        # that produces OVA-style OVF which Engine's ConfigurationType.OVF
        # restore path does not parse as native oVirt OVF.
        matches = vms_service.list(
            search="id=%s" % vm_id,
            all_content=True,
        )
        exact = [item for item in matches if item.id == vm_id]
        if exact:
            ovf = _extract_ovf(exact[0])

    if not ovf:
        LOG.warning("Engine returned no native oVirt OVF data")
        return snapshot_id, False

    data = ovf if isinstance(ovf, bytes) else str(ovf).encode("utf-8")
    with open(target, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())

    LOG.info("Saved VM OVF: %s (%d bytes)", target, len(data))
    return snapshot_id, True


class LogProgress:
    def __init__(self, disk_id, interval=10, percent_step=5):
        self.disk_id = disk_id
        self.interval = max(1, int(interval))
        self.percent_step = max(1, int(percent_step))
        self._size = None
        self._done = 0
        self._started = time.monotonic()
        self._last_log = self._started
        self._last_percent = -self.percent_step
        self._lock = threading.Lock()

    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value):
        with self._lock:
            self._size = value
            if value:
                LOG.info(
                    "Download size for disk %s: %.2f GiB",
                    self.disk_id,
                    value / 1024**3,
                )

    def update(self, amount):
        with self._lock:
            self._done += amount
            now = time.monotonic()
            elapsed = max(now - self._started, 0.001)
            rate = self._done / elapsed

            percent = None
            if self._size:
                percent = min(100.0, self._done * 100.0 / self._size)

            due_time = now - self._last_log >= self.interval
            due_percent = (
                percent is not None
                and percent >= self._last_percent + self.percent_step
            )
            finished = percent is not None and percent >= 100.0

            if not (due_time or due_percent or finished):
                return

            if self._size:
                remaining = max(self._size - self._done, 0)
                eta = remaining / rate if rate > 0 else 0
                LOG.info(
                    "Download progress disk %s: %.1f%%, %.2f/%.2f GiB, "
                    "%.1f MiB/s, ETA %ds",
                    self.disk_id,
                    percent,
                    self._done / 1024**3,
                    self._size / 1024**3,
                    rate / 1024**2,
                    int(eta),
                )
                self._last_percent = int(percent // self.percent_step) * self.percent_step
            else:
                LOG.info(
                    "Download progress disk %s: %.2f GiB, %.1f MiB/s",
                    self.disk_id,
                    self._done / 1024**3,
                    rate / 1024**2,
                )

            self._last_log = now


def create_transfer(connection, backup_id, disk_id, cp):
    transfers = connection.system_service().image_transfers_service()
    policy = cp.get(
        "config", "imageio_timeout_policy", fallback="cancel"
    )
    timeout = cp.getint("config", "imageio_timeout", fallback=120)

    transfer = transfers.add(
        types.ImageTransfer(
            disk=types.Disk(id=disk_id),
            backup=types.Backup(id=backup_id),
            direction=types.ImageTransferDirection.DOWNLOAD,
            format=types.DiskFormat.RAW,
            timeout_policy=types.ImageTransferTimeoutPolicy(policy),
        )
    )
    service = transfers.image_transfer_service(transfer.id)
    started = time.monotonic()

    while True:
        time.sleep(1)
        transfer = service.get()

        if transfer.phase == types.ImageTransferPhase.TRANSFERRING:
            LOG.info("ImageTransfer %s ready for disk %s", transfer.id, disk_id)
            return transfer

        if transfer.phase == types.ImageTransferPhase.FINISHED_FAILURE:
            raise RuntimeError("ImageTransfer %s failed" % transfer.id)

        if transfer.phase == types.ImageTransferPhase.PAUSED_SYSTEM:
            try:
                service.cancel()
            finally:
                raise RuntimeError("ImageTransfer %s paused by system" % transfer.id)

        if transfer.phase != types.ImageTransferPhase.INITIALIZING:
            try:
                service.cancel()
            finally:
                raise RuntimeError(
                    "Unexpected ImageTransfer phase %s for %s"
                    % (transfer.phase, transfer.id)
                )

        if time.monotonic() - started > timeout:
            try:
                service.cancel()
            finally:
                raise RuntimeError(
                    "Timeout initializing ImageTransfer %s" % transfer.id
                )


def finalize_transfer(connection, transfer, disk_id, cp):
    timeout = cp.getint("config", "imageio_timeout", fallback=120)
    service = (
        connection.system_service()
        .image_transfers_service()
        .image_transfer_service(transfer.id)
    )
    service.finalize()
    started = time.monotonic()

    while True:
        time.sleep(1)
        try:
            current = service.get()
        except sdk.NotFoundError:
            disk = (
                connection.system_service()
                .disks_service()
                .disk_service(disk_id)
                .get()
            )
            if disk.status == types.DiskStatus.OK:
                return
            raise RuntimeError(
                "Transfer %s disappeared; disk %s status=%s"
                % (transfer.id, disk_id, disk.status)
            )

        if current.phase == types.ImageTransferPhase.FINISHED_SUCCESS:
            return
        if current.phase == types.ImageTransferPhase.FINISHED_FAILURE:
            raise RuntimeError(
                "ImageTransfer %s failed while finalizing" % transfer.id
            )
        if time.monotonic() - started > timeout:
            raise RuntimeError(
                "Timeout finalizing ImageTransfer %s" % transfer.id
            )


def download_disk(connection, backup, disk, disk_dir, cp):
    disk_id = disk.id
    target = disk_dir / ("%s.qcow2" % disk_id)
    transfer = create_transfer(connection, backup.id, disk_id, cp)
    download_started = time.monotonic()

    try:
        kwargs = {
            "fmt": "qcow2",
            "secure": cp.getboolean("config", "verify_tls", fallback=True),
            "buffer_size": imageio_client.BUFFER_SIZE,
            "max_workers": cp.getint(
                "config", "imageio_max_workers", fallback=4
            ),
        }
        signature = inspect.signature(imageio_client.download)
        if "proxy_url" in signature.parameters:
            kwargs["proxy_url"] = getattr(transfer, "proxy_url", None)
        if "progress" in signature.parameters:
            kwargs["progress"] = LogProgress(
                disk_id,
                interval=cp.getint(
                    "config", "imageio_progress_interval", fallback=10
                ),
                percent_step=cp.getint(
                    "config", "imageio_progress_percent", fallback=5
                ),
            )

        LOG.info("Downloading disk %s -> %s", disk_id, target)
        imageio_client.download(
            transfer.transfer_url,
            str(target),
            cp.get(
                "config",
                "ca_file",
                fallback="/etc/pki/ovirt-engine/ca.pem",
            ),
            **kwargs
        )
    finally:
        finalize_transfer(connection, transfer, disk_id, cp)

    download_seconds = time.monotonic() - download_started

    LOG.info("Checking qcow2 disk %s with qemu-img", disk_id)
    subprocess.run(
        ["qemu-img", "check", "-q", str(target)],
        check=True,
    )
    info = json.loads(
        subprocess.check_output(
            ["qemu-img", "info", "--output=json", str(target)],
            text=True,
        )
    )

    LOG.info(
        "Disk %s completed: virtual=%.2f GiB actual=%.2f GiB "
        "download_time=%.1fs",
        disk_id,
        (info.get("virtual-size") or 0) / 1024**3,
        (info.get("actual-size") or 0) / 1024**3,
        download_seconds,
    )

    return {
        "id": disk_id,
        "file": "disks/%s" % target.name,
        "format": info.get("format"),
        "virtual_size": info.get("virtual-size"),
        "actual_size": info.get("actual-size"),
        "transfer_id": transfer.id,
        "download_seconds": round(download_seconds, 3),
    }


def write_manifest(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


def cleanup_retention(vm_dir, cp):
    completed = sorted(
        p for p in vm_dir.iterdir()
        if p.is_dir()
        and not p.name.endswith(".partial")
        and ".failed-" not in p.name
    )

    days_raw = cp.get("config", "backup_keep_count", fallback="").strip()
    if days_raw:
        cutoff = dt.datetime.now() - dt.timedelta(days=int(days_raw))
        for path in list(completed):
            try:
                stamp = dt.datetime.strptime(path.name, "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            if stamp < cutoff:
                LOG.info("Retention deleting by age: %s", path)
                shutil.rmtree(path)
                completed.remove(path)

    count_raw = cp.get(
        "config", "backup_keep_count_by_number", fallback=""
    ).strip()
    if count_raw:
        keep = int(count_raw)
        completed.sort(key=lambda p: p.name)
        while len(completed) > keep:
            path = completed.pop(0)
            LOG.info("Retention deleting by count: %s", path)
            shutil.rmtree(path)


def backup_vm(connection, base, vm_name, cp, dry_run):
    vm_started = time.monotonic()
    system = connection.system_service()
    vms = system.vms_service()
    vm = find_vm(vms, vm_name)
    vm_service = vms.vm_service(vm.id)

    api_disks, disk_meta = select_disks(system, vm_service, cp)
    LOG.info("Start native FULL backup for: %s", vm_name)
    for disk in disk_meta:
        LOG.info(
            "Selected disk %s bootable=%s provisioned=%.2f GiB",
            disk["id"],
            disk["bootable"],
            (disk["provisioned_size"] or 0) / 1024**3,
        )

    check_destination_space(base, disk_meta, cp)

    if dry_run:
        LOG.info("Dry-run: no backup API call for %s", vm_name)
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    vm_dir = base / vm_name
    vm_dir.mkdir(parents=True, exist_ok=True)
    work = vm_dir / (stamp + ".partial")
    final = vm_dir / stamp

    if work.exists() or final.exists():
        raise RuntimeError("Backup directory already exists for %s" % stamp)

    work.mkdir()
    disks_dir = work / "disks"
    disks_dir.mkdir()

    manifest = {
        "schema": 1,
        "type": "full",
        "status": "running",
        "vm_name": vm.name,
        "vm_id": vm.id,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "selected_disks": disk_meta,
        "nics": collect_nics(vm_service),
        "files": [],
    }
    write_manifest(work / "manifest.json", manifest)

    backup = None
    backup_service = None
    finalize_requested = False

    try:
        backups = vm_service.backups_service()
        backup = backups.add(
            types.Backup(
                disks=api_disks,
                description="oVirtBackup native FULL %s" % stamp,
            ),
            require_consistency=cp.getboolean(
                "config", "require_consistency", fallback=False
            ),
            use_active=False,
        )
        backup_service = backups.backup_service(backup.id)
        LOG.info("Engine backup ID: %s", backup.id)

        operation_timeout = cp.getint(
            "config", "backup_operation_timeout", fallback=3600
        )
        backup = wait_backup(
            backup_service,
            backup,
            types.BackupPhase.READY,
            operation_timeout,
        )
        LOG.info("Engine backup %s READY", backup.id)

        manifest["backup_id"] = backup.id
        manifest["from_checkpoint_id"] = getattr(
            backup, "from_checkpoint_id", None
        )
        manifest["to_checkpoint_id"] = getattr(
            backup, "to_checkpoint_id", None
        )

        snapshot_id, ovf_saved = save_ovf(
            vms, vm_service, vm.id, backup, work / "vm.ovf"
        )
        manifest["snapshot_id"] = snapshot_id
        manifest["ovf_saved"] = ovf_saved
        write_manifest(work / "manifest.json", manifest)

        backup_disks = backup_service.disks_service().list()
        wanted = {d.id for d in api_disks}
        returned = {d.id for d in backup_disks}
        if wanted != returned:
            raise RuntimeError(
                "Engine backup disk set differs: requested=%s returned=%s"
                % (sorted(wanted), sorted(returned))
            )

        for disk in backup_disks:
            manifest["files"].append(
                download_disk(connection, backup, disk, disks_dir, cp)
            )
            write_manifest(work / "manifest.json", manifest)

        backup_service.finalize()
        finalize_requested = True
        backup = wait_backup(
            backup_service,
            backup,
            types.BackupPhase.SUCCEEDED,
            operation_timeout,
        )

        manifest["status"] = "complete"
        manifest["completed_utc"] = dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
        manifest["duration_seconds"] = round(
            time.monotonic() - vm_started, 3
        )
        write_manifest(work / "manifest.json", manifest)

        os.rename(work, final)
        LOG.info(
            "Backup complete for %s: %s (%.1fs)",
            vm_name,
            final,
            manifest["duration_seconds"],
        )
        cleanup_retention(vm_dir, cp)

    except Exception:
        manifest["status"] = "failed"
        manifest["failed_utc"] = dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
        try:
            write_manifest(work / "manifest.json", manifest)
        except Exception:
            LOG.exception("Cannot update failed manifest")

        if backup_service is not None and not finalize_requested:
            try:
                backup_service.finalize()
            except Exception:
                LOG.exception(
                    "Cannot finalize Engine backup %s after failure",
                    getattr(backup, "id", None),
                )

        if work.exists():
            failed = vm_dir / (
                stamp + ".failed-" + time.strftime("%H%M%S")
            )
            try:
                os.rename(work, failed)
                LOG.error("Incomplete backup kept at %s", failed)
            except Exception:
                LOG.exception("Cannot rename incomplete backup")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Native oVirt FULL backup via VM Backup API"
    )
    parser.add_argument("-c", "--config-file", required=True)
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only preflight; do not start Engine backup",
    )
    parser.add_argument(
        "--vm-names",
        help='Override VM list as JSON, e.g. ["1c-linux"]',
    )
    args = parser.parse_args()

    cp = load_config(args.config_file)
    configure_logging(cp, args.debug)
    base = destination_preflight(cp)

    config_dry = cp.getboolean("config", "dry_run", fallback=False)
    dry_run = args.dry_run or config_dry

    connection = connect(cp)
    failures = []
    try:
        system = connection.system_service()
        names = select_vm_names(system, cp, args.vm_names)
        if not names:
            raise RuntimeError("No VMs selected")

        LOG.info("Native backup destination: %s", base)
        LOG.info("Selected VMs: %s", ", ".join(names))

        for name in names:
            try:
                backup_vm(connection, base, name, cp, dry_run)
            except Exception as exc:
                failures.append((name, str(exc)))
                LOG.exception("Backup failed for %s", name)

        if failures:
            LOG.error("Completed with %d failure(s)", len(failures))
            for name, error in failures:
                LOG.error("  %s: %s", name, error)
            return 1

        LOG.info("All native backups completed successfully")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
