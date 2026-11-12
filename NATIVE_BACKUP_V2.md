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
- The web console uses the same native scripts and operation lock as CLI runs.


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
- NIC restore is explicit: none, isolated (new MAC/link down),
  isolated-original-mac (saved MAC/link down), or original
  (saved MAC/profile/link state for DR);
- the VM is never started automatically;
- restore state is recorded under `/var/lib/ovirt-backup/restores`;
- failed restore resources are kept for diagnosis rather than deleted automatically.

This allows a restore test beside the running source VM without duplicate MAC or
IP conflicts.


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


## Logging and restore metrics

Interactive native backup and restore runs log to both the terminal and files.
The example configuration uses:

- backup: `/var/log/ovirt-backup/backup-native.log`;
- restore: `/var/log/ovirt-backup/restore-native.log`.

Completed restore state JSON records total `duration_seconds`. Each restored
disk records `upload_seconds` and `upload_virtual_mib_per_second`; the latter
is based on the virtual image size, so it is directly comparable with ImageIO
progress for sparse RAW images.


## Network restore modes

Native restore supports four NIC modes:

- `none`: no NICs.
- `isolated`: recreate NIC metadata with an Engine-assigned MAC and
  `linked=false`.
- `isolated-original-mac`: recreate NIC metadata with the saved MAC and
  `linked=false`. If that MAC already exists in Engine, preflight requires
  the target cluster MAC pool to allow duplicate MAC addresses.
- `original`: disaster-recovery mode that restores the saved MAC/profile and
  original link state; it is rejected while the source VM still exists in the
  same Engine.

The VM is never started automatically in any mode.
