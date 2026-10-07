#!/usr/bin/env python3
"""
Restore a native FULL oVirt backup into a new VM.

Safety defaults:
- preflight only unless --execute is supplied;
- target VM name must not already exist;
- VM is created without NICs;
- restored VM is never started automatically;
- original backup files are never modified.
"""

import argparse
import configparser
import datetime as dt
import inspect
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import ovirtsdk4 as sdk
import ovirtsdk4.types as types
from ovirt_imageio import client as imageio_client


LOG = logging.getLogger("ovirt-native-restore")


def load_config(path):
    cp = configparser.RawConfigParser()
    with open(path, "r", encoding="utf-8") as fh:
        cp.read_file(fh)
    if not cp.has_section("config"):
        raise RuntimeError("Missing [config] section")
    return cp


def configure_logging(debug):
    logging.basicConfig(
        format="%(asctime)s: %(message)s",
        level=logging.DEBUG if debug else logging.INFO,
    )


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


def local_name(value):
    return value.rsplit("}", 1)[-1].split(":", 1)[-1]


def get_attr(element, name):
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return value
    return None


def element_text(element, name):
    for child in list(element):
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def load_backup(backup_dir):
    backup_dir = Path(backup_dir).resolve()
    manifest_path = backup_dir / "manifest.json"
    ovf_path = backup_dir / "vm.ovf"

    if not manifest_path.is_file():
        raise RuntimeError("Missing manifest.json: %s" % manifest_path)
    if not ovf_path.is_file():
        raise RuntimeError("Missing vm.ovf: %s" % ovf_path)

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    if manifest.get("status") != "complete":
        raise RuntimeError(
            "Backup is not complete: status=%r" % manifest.get("status")
        )
    if manifest.get("type") != "full":
        raise RuntimeError(
            "restore_native.py currently supports FULL backups only"
        )

    files = manifest.get("files") or []
    selected = {
        item["id"]: item for item in (manifest.get("selected_disks") or [])
    }

    if not files:
        raise RuntimeError("Backup manifest contains no disk files")

    disks = []
    for item in files:
        disk_id = item.get("id")
        rel = item.get("file")
        if not disk_id or not rel:
            raise RuntimeError("Invalid disk entry in manifest: %r" % item)
        path = (backup_dir / rel).resolve()
        if backup_dir not in path.parents:
            raise RuntimeError("Disk path escapes backup directory: %s" % rel)
        if not path.is_file():
            raise RuntimeError("Backup disk file is missing: %s" % path)
        metadata = selected.get(disk_id)
        if metadata is None:
            raise RuntimeError(
                "Disk %s is missing from selected_disks metadata" % disk_id
            )
        disks.append({
            "source_id": disk_id,
            "path": path,
            "manifest": item,
            "metadata": metadata,
        })

    return backup_dir, manifest, ovf_path, disks


def qemu_info(path):
    return json.loads(
        subprocess.check_output(
            ["qemu-img", "info", "--output=json", str(path)],
            text=True,
        )
    )


def qemu_check(path):
    LOG.info("Checking backup image: %s", path)
    subprocess.run(["qemu-img", "check", "-q", str(path)], check=True)


def find_exact(service, name, kind):
    matches = service.list(search="name=%s" % name)
    exact = [obj for obj in matches if getattr(obj, "name", None) == name]
    if not exact:
        raise RuntimeError("%s %r not found" % (kind, name))
    if len(exact) != 1:
        raise RuntimeError("%s name %r is not unique" % (kind, name))
    return exact[0]


def ensure_vm_absent(vms_service, name):
    matches = vms_service.list(search="name=%s" % name)
    exact = [vm for vm in matches if vm.name == name]
    if exact:
        raise RuntimeError(
            "Target VM %r already exists (id=%s)" % (name, exact[0].id)
        )


def ovf_disk_boot_order(root, disk_id):
    for item in root.iter():
        if local_name(item.tag) != "Item":
            continue
        instance = element_text(item, "InstanceId")
        if instance != disk_id:
            continue
        if element_text(item, "ResourceType") != "17":
            continue
        raw = element_text(item, "BootOrder")
        if raw:
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def sanitize_ovf(ovf_path, target_name):
    data = ovf_path.read_bytes()

    # Preserve the original namespace prefixes. This matters because OVF
    # contains QName values such as xsi:type="ovf:VirtualSystem_Type".
    # ElementTree otherwise may serialize the namespace as ns0 while leaving
    # the QName value unchanged.
    seen_namespaces = set()
    for _event, ns in ET.iterparse(
        io.BytesIO(data), events=("start-ns",)
    ):
        prefix, uri = ns
        key = (prefix or "", uri)
        if key in seen_namespaces:
            continue
        seen_namespaces.add(key)
        ET.register_namespace(prefix or "", uri)

    root = ET.fromstring(data)

    boot_orders = {}
    disk_ids = []
    for element in root.iter():
        if local_name(element.tag) == "Disk":
            disk_id = get_attr(element, "diskId")
            if disk_id:
                disk_ids.append(disk_id)
                boot_orders[disk_id] = ovf_disk_boot_order(root, disk_id)

    removed_sections = 0
    removed_items = 0
    removed_references = 0

    for parent in list(root.iter()):
        for child in list(parent):
            lname = local_name(child.tag)

            if lname == "References":
                parent.remove(child)
                removed_references += 1
                continue

            if lname in ("DiskSection", "NetworkSection"):
                parent.remove(child)
                removed_sections += 1
                continue

            if lname == "Section":
                section_type = get_attr(child, "type") or ""
                if (
                    section_type.endswith("DiskSection_Type")
                    or section_type.endswith("NetworkSection_Type")
                ):
                    parent.remove(child)
                    removed_sections += 1
                    continue

            if lname == "Item":
                resource_type = element_text(child, "ResourceType")
                device_type = (element_text(child, "Type") or "").lower()
                if resource_type in ("10", "17") or device_type in (
                    "interface", "disk"
                ):
                    parent.remove(child)
                    removed_items += 1

    name_changed = False
    name_parent = None

    # oVirt OVF produced by different engine/import paths may represent the
    # virtual machine container as either Content or VirtualSystem.  Rename
    # only a direct child Name of that VM container; never touch hardware,
    # network or disk names nested deeper in the document.
    for content in root.iter():
        if local_name(content.tag) not in ("Content", "VirtualSystem"):
            continue
        for child in list(content):
            if local_name(child.tag) == "Name":
                child.text = target_name
                name_changed = True
                name_parent = local_name(content.tag)
                break
        if name_changed:
            break

    if not name_changed:
        containers = sorted({
            local_name(element.tag)
            for element in root.iter()
            if local_name(element.tag) in ("Content", "VirtualSystem")
        })
        raise RuntimeError(
            "Cannot find direct VM Name element in OVF "
            "(containers found: %s)"
            % (", ".join(containers) if containers else "none")
        )

    # Safety validation: the isolated restore VM must not retain any OVF
    # disk/network declarations. Disks are recreated and attached explicitly
    # after the VM shell is created, and NICs are intentionally omitted.
    leftovers = []
    for element in root.iter():
        lname = local_name(element.tag)
        if lname in ("DiskSection", "NetworkSection", "Disk"):
            leftovers.append(lname)
            continue
        if lname == "Section":
            section_type = get_attr(element, "type") or ""
            if (
                section_type.endswith("DiskSection_Type")
                or section_type.endswith("NetworkSection_Type")
            ):
                leftovers.append(section_type)

    if leftovers:
        raise RuntimeError(
            "Unsafe sanitized OVF still contains disk/network declarations: %s"
            % ", ".join(sorted(set(leftovers)))
        )

    LOG.info(
        "Sanitized OVF VM name in %s container -> %s",
        name_parent,
        target_name,
    )

    sanitized = ET.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    ET.fromstring(sanitized)

    return sanitized, {
        "disk_ids": disk_ids,
        "boot_orders": boot_orders,
        "removed_sections": removed_sections,
        "removed_items": removed_items,
        "removed_references": removed_references,
    }


def wait_disk_ok(system, disk_id, timeout):
    service = system.disks_service().disk_service(disk_id)
    started = time.monotonic()
    last = None

    while True:
        disk = service.get()
        if disk.status == types.DiskStatus.OK:
            return disk
        if disk.status != last:
            LOG.info("Disk %s status: %s", disk_id, disk.status)
            last = disk.status
        if time.monotonic() - started > timeout:
            raise RuntimeError(
                "Timeout waiting for disk %s: status=%s"
                % (disk_id, disk.status)
            )
        time.sleep(1)


class LogProgress:
    def __init__(self, label, interval=10, percent_step=5):
        self.label = label
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
                    "Upload size %s: %.2f GiB",
                    self.label,
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
                    "Upload progress %s: %.1f%%, %.2f/%.2f GiB, "
                    "%.1f MiB/s, ETA %ds",
                    self.label,
                    percent,
                    self._done / 1024**3,
                    self._size / 1024**3,
                    rate / 1024**2,
                    int(eta),
                )
                self._last_percent = (
                    int(percent // self.percent_step) * self.percent_step
                )
            else:
                LOG.info(
                    "Upload progress %s: %.2f GiB, %.1f MiB/s",
                    self.label,
                    self._done / 1024**3,
                    rate / 1024**2,
                )

            self._last_log = now


def create_upload_transfer(connection, disk_id, cp):
    transfers = connection.system_service().image_transfers_service()
    policy = cp.get(
        "config", "imageio_timeout_policy", fallback="cancel"
    )
    timeout = cp.getint("config", "imageio_timeout", fallback=120)

    transfer = transfers.add(
        types.ImageTransfer(
            disk=types.Disk(id=disk_id),
            direction=types.ImageTransferDirection.UPLOAD,
            timeout_policy=types.ImageTransferTimeoutPolicy(policy),
        )
    )
    service = transfers.image_transfer_service(transfer.id)
    started = time.monotonic()

    while True:
        time.sleep(1)
        transfer = service.get()

        if transfer.phase == types.ImageTransferPhase.TRANSFERRING:
            LOG.info(
                "Upload ImageTransfer %s ready for disk %s",
                transfer.id,
                disk_id,
            )
            return transfer

        if transfer.phase == types.ImageTransferPhase.FINISHED_FAILURE:
            raise RuntimeError("ImageTransfer %s failed" % transfer.id)

        if transfer.phase == types.ImageTransferPhase.PAUSED_SYSTEM:
            try:
                service.cancel()
            finally:
                raise RuntimeError(
                    "ImageTransfer %s paused by system" % transfer.id
                )

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


def upload_image(connection, path, disk_id, cp):
    transfer = create_upload_transfer(connection, disk_id, cp)
    started = time.monotonic()

    try:
        signature = inspect.signature(imageio_client.upload)
        kwargs = {
            "secure": cp.getboolean("config", "verify_tls", fallback=True),
            "buffer_size": imageio_client.BUFFER_SIZE,
            "max_workers": cp.getint(
                "config", "imageio_max_workers", fallback=4
            ),
        }
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
        if "disk_is_zero" in signature.parameters:
            kwargs["disk_is_zero"] = True

        LOG.info("Uploading %s -> disk %s", path, disk_id)
        imageio_client.upload(
            str(path),
            transfer.transfer_url,
            cp.get(
                "config",
                "ca_file",
                fallback="/etc/pki/ovirt-engine/ca.pem",
            ),
            **kwargs
        )
    finally:
        finalize_transfer(connection, transfer, disk_id, cp)

    elapsed = time.monotonic() - started
    LOG.info(
        "Upload complete for disk %s in %.1fs", disk_id, elapsed
    )
    return transfer.id, elapsed


def wait_vm_down(vm_service, timeout):
    started = time.monotonic()
    last = None
    while True:
        vm = vm_service.get()
        if vm.status == types.VmStatus.DOWN:
            return vm
        if vm.status != last:
            LOG.info("VM %s status: %s", vm.id, vm.status)
            last = vm.status
        if time.monotonic() - started > timeout:
            raise RuntimeError(
                "Timeout waiting for VM %s: status=%s"
                % (vm.id, vm.status)
            )
        time.sleep(1)


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(
        description="Restore native FULL backup into a new isolated VM"
    )
    parser.add_argument("-c", "--config-file", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--target-name")
    parser.add_argument("--cluster")
    parser.add_argument("--storage-domain")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform restore. Without this flag only preflight is run.",
    )
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    configure_logging(args.debug)
    cp = load_config(args.config_file)

    backup_dir, manifest, ovf_path, disks = load_backup(args.backup_dir)
    source_name = manifest.get("vm_name") or "restored-vm"
    target_name = args.target_name or (source_name + "_RESTORE_TEST")
    cluster_name = args.cluster or cp.get(
        "config", "cluster_name", fallback="Default"
    )
    storage_name = args.storage_domain or cp.get(
        "config", "storage_domain", fallback=""
    )
    if not storage_name:
        raise RuntimeError(
            "--storage-domain is required because storage_domain is not configured"
        )

    sanitized_ovf, ovf_meta = sanitize_ovf(ovf_path, target_name)

    LOG.info("Backup: %s", backup_dir)
    LOG.info("Source VM: %s (%s)", source_name, manifest.get("vm_id"))
    LOG.info("Target VM: %s", target_name)
    LOG.info("Target cluster: %s", cluster_name)
    LOG.info("Target storage domain: %s", storage_name)
    LOG.info(
        "Sanitized OVF: removed references=%d sections=%d hardware_items=%d",
        ovf_meta["removed_references"],
        ovf_meta["removed_sections"],
        ovf_meta["removed_items"],
    )

    for item in disks:
        info = qemu_info(item["path"])
        item["qemu_info"] = info
        item["ovf_boot_order"] = ovf_meta["boot_orders"].get(
            item["source_id"]
        )
        LOG.info(
            "Backup disk %s: file=%s format=%s virtual=%.2f GiB "
            "actual=%.2f GiB interface=%s boot_order=%s",
            item["source_id"],
            item["path"],
            info.get("format"),
            (info.get("virtual-size") or 0) / 1024**3,
            (info.get("actual-size") or 0) / 1024**3,
            item["metadata"].get("interface"),
            item["ovf_boot_order"],
        )
        qemu_check(item["path"])

    connection = connect(cp)
    try:
        system = connection.system_service()
        vms_service = system.vms_service()
        ensure_vm_absent(vms_service, target_name)

        cluster = find_exact(
            system.clusters_service(), cluster_name, "Cluster"
        )
        storage = find_exact(
            system.storage_domains_service(),
            storage_name,
            "Storage domain",
        )

        needed = sum(
            item["qemu_info"].get("virtual-size") or 0 for item in disks
        )
        available = getattr(storage, "available", None)
        if available is not None:
            LOG.info(
                "Storage domain free: %.2f GiB; restore virtual size: %.2f GiB",
                available / 1024**3,
                needed / 1024**3,
            )
            if available < needed:
                raise RuntimeError(
                    "Not enough space on storage domain %s" % storage_name
                )

        LOG.info(
            "Preflight OK: VM absent, cluster=%s, storage=%s",
            cluster.id,
            storage.id,
        )

        if not args.execute:
            LOG.info(
                "Preflight only. Re-run with --execute to perform restore."
            )
            return 0

        state_dir = Path("/var/lib/ovirt-backup/restores")
        state_path = state_dir / (
            "%s-%s.json"
            % (target_name, time.strftime("%Y%m%d_%H%M%S"))
        )
        sanitized_path = state_path.with_suffix(".ovf")
        state_dir.mkdir(parents=True, exist_ok=True)

        with open(sanitized_path, "wb") as fh:
            fh.write(sanitized_ovf)

        state = {
            "schema": 1,
            "status": "running",
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "backup_dir": str(backup_dir),
            "source_vm_name": source_name,
            "source_vm_id": manifest.get("vm_id"),
            "target_vm_name": target_name,
            "cluster_name": cluster_name,
            "cluster_id": cluster.id,
            "storage_domain_name": storage_name,
            "storage_domain_id": storage.id,
            "sanitized_ovf": str(sanitized_path),
            "network_interfaces_restored": False,
            "vm_started": False,
            "created_disks": [],
        }
        write_state(state_path, state)
        LOG.info("Restore state: %s", state_path)

        vm = vms_service.add(
            types.Vm(
                name=target_name,
                cluster=types.Cluster(id=cluster.id),
                initialization=types.Initialization(
                    configuration=types.Configuration(
                        type=types.ConfigurationType.OVF,
                        data=sanitized_ovf.decode("utf-8"),
                    )
                ),
            )
        )
        state["target_vm_id"] = vm.id
        write_state(state_path, state)
        LOG.info("Created isolated VM %s id=%s", target_name, vm.id)

        vm_service = vms_service.vm_service(vm.id)
        wait_vm_down(
            vm_service,
            cp.getint("config", "backup_operation_timeout", fallback=3600),
        )

        nics = vm_service.nics_service().list()
        for nic in nics:
            LOG.warning(
                "Removing unexpected NIC %s from restored VM", nic.id
            )
            vm_service.nics_service().nic_service(nic.id).remove()

        for item in disks:
            meta = item["metadata"]
            info = item["qemu_info"]
            alias = meta.get("alias") or meta.get("name") or "restored-disk"
            restored_alias = "%s_RESTORE" % alias

            disk_format = types.DiskFormat(
                (meta.get("format") or "raw").lower()
            )
            disk = system.disks_service().add(
                types.Disk(
                    alias=restored_alias,
                    name=restored_alias,
                    format=disk_format,
                    sparse=bool(meta.get("sparse")),
                    provisioned_size=info.get("virtual-size"),
                    storage_domains=[
                        types.StorageDomain(id=storage.id)
                    ],
                )
            )
            LOG.info(
                "Created target disk %s for source disk %s",
                disk.id,
                item["source_id"],
            )

            state_disk = {
                "source_disk_id": item["source_id"],
                "target_disk_id": disk.id,
                "alias": restored_alias,
                "file": str(item["path"]),
                "status": "created",
            }
            state["created_disks"].append(state_disk)
            write_state(state_path, state)

            wait_disk_ok(
                system,
                disk.id,
                cp.getint(
                    "config", "backup_operation_timeout", fallback=3600
                ),
            )

            transfer_id, elapsed = upload_image(
                connection, item["path"], disk.id, cp
            )
            state_disk["transfer_id"] = transfer_id
            state_disk["upload_seconds"] = round(elapsed, 3)
            state_disk["status"] = "uploaded"
            write_state(state_path, state)

            interface = types.DiskInterface(
                (meta.get("interface") or "virtio").lower()
            )
            bootable = bool(meta.get("bootable"))
            if item["ovf_boot_order"] is not None:
                bootable = item["ovf_boot_order"] > 0
            elif len(disks) == 1 and not bootable:
                LOG.warning(
                    "No boot flag found for only disk; marking it bootable "
                    "for restore test"
                )
                bootable = True

            attachment = vm_service.disk_attachments_service().add(
                types.DiskAttachment(
                    disk=types.Disk(id=disk.id),
                    interface=interface,
                    active=True,
                    bootable=bootable,
                    read_only=False,
                )
            )
            state_disk["attachment_id"] = attachment.id
            state_disk["interface"] = str(interface)
            state_disk["bootable"] = bootable
            state_disk["status"] = "attached"
            write_state(state_path, state)
            LOG.info(
                "Attached disk %s to VM %s interface=%s bootable=%s",
                disk.id,
                target_name,
                interface,
                bootable,
            )

        final_vm = vm_service.get()
        final_nics = vm_service.nics_service().list()
        attachments = vm_service.disk_attachments_service().list()

        if final_nics:
            raise RuntimeError(
                "Restored VM unexpectedly has %d NIC(s)" % len(final_nics)
            )

        restored_ids = {d["target_disk_id"] for d in state["created_disks"]}
        attached_ids = {att.disk.id for att in attachments}
        if not restored_ids.issubset(attached_ids):
            raise RuntimeError(
                "Not all restored disks are attached: restored=%s attached=%s"
                % (sorted(restored_ids), sorted(attached_ids))
            )

        state["status"] = "complete"
        state["completed_utc"] = dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
        state["vm_status"] = str(final_vm.status)
        write_state(state_path, state)

        LOG.info(
            "Restore complete: VM %s (%s) status=%s, NICs=0. "
            "VM was NOT started.",
            target_name,
            final_vm.id,
            final_vm.status,
        )
        LOG.info("Restore state saved: %s", state_path)
        return 0

    except Exception:
        LOG.exception(
            "Restore failed. Created resources are intentionally kept "
            "for diagnosis; use restore state JSON for cleanup."
        )
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
