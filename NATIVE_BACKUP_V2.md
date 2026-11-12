# Native Backup v2

The native-backup-v2 branch replaces the legacy Export Domain workflow with
the official oVirt VM Backup API and ImageTransfer.

Current flow:

select VM and disks
-> VmBackupsService.add()
-> wait for BackupPhase.READY
-> save backup snapshot OVF
-> ImageTransfer DOWNLOAD to sparse RAW
-> validate RAW image metadata
-> finalize ImageTransfer
-> finalize VM Backup
-> retention

The pioner branch remains the rollback path.

## Safety rules

- Full backups only in the first implementation.
- FULL backup disk artifacts are stored as sparse RAW. This avoids qcow2
  random-read amplification when restoring from NFS. Incremental backup will
  require qcow2 and is intentionally outside the current FULL-only flow.
- A backup directory becomes complete only after Engine backup finalization.
- In-progress directories end in .partial.
- Failed runs are retained as .failed-* for diagnostics.
- Retention touches only completed timestamp directories.
- The destination must already exist.
- backup_path_must_be_mount=True prevents accidental writes to the Engine root
  filesystem if an NFS mount disappears.
- The legacy Export Domain is not used.
- The web console is read-only until backup and restore are validated.


## Restore prototype

`restore_native.py` restores one completed FULL backup into a new VM.

Safety defaults:

- preflight-only unless `--execute` is supplied;
- target VM name must not already exist;
- disk and NIC entries are stripped from the saved OVF before VM creation;
- backup disks are recreated and uploaded through ImageTransfer;
- old completed qcow2 backups remain restorable; RAW and qcow2 are detected
  from the backup file metadata at restore time;
- restored disks are attached after upload;
- no NICs are restored;
- the VM is never started automatically;
- restore state is recorded under `/var/lib/ovirt-backup/restores`;
- failed restore resources are kept for diagnosis rather than deleted automatically.

This allows a restore test beside the running source VM without duplicate MAC or
IP conflicts.


### Network restore modes

`restore_native.py` supports three explicit network modes. The VM is never
started automatically in any mode.

- `--network-mode none`: do not create NICs.
- `--network-mode isolated`: recreate NIC count, names, interfaces and vNIC
  profiles, let Engine allocate new MAC addresses, and force link down.
- `--network-mode original`: recreate NICs with the saved original MAC
  addresses and profiles. This mode is for real disaster recovery and refuses
  to run while the source VM still exists in the same Engine.

Native backups store NIC metadata in manifest.json so DHCP reservations tied to
MAC addresses can be restored exactly.


## ImageTransfer host selection

When `imageio_network` is configured, native backup/restore automatically
selects the oVirt host closest to each host-local NFS storage domain:

- backup: each source disk is evaluated separately;
- restore: the selected target storage domain is evaluated;
- `storage.address` is matched to the IPv4 address of an UP oVirt host on
  `imageio_network`;
- if there is no unique match (for example an external Synology NFS server),
  host selection is left to Engine instead of guessing.

No fixed `imageio_download_host` or `imageio_upload_host` setting is needed.

Native backup and restore also share
`/var/lib/ovirt-backup/operation.lock`, so two native heavy I/O operations
do not run concurrently. The legacy `pioner` branch is intentionally
unchanged during migration.
