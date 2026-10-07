# Native Backup v2

The native-backup-v2 branch replaces the legacy Export Domain workflow with
the official oVirt VM Backup API and ImageTransfer.

Current flow:

select VM and disks
-> VmBackupsService.add()
-> wait for BackupPhase.READY
-> save backup snapshot OVF
-> ImageTransfer DOWNLOAD to qcow2
-> qemu-img check
-> finalize ImageTransfer
-> finalize VM Backup
-> retention

The pioner branch remains the rollback path.

## Safety rules

- Full backups only in the first implementation.
- A backup directory becomes complete only after Engine backup finalization.
- In-progress directories end in .partial.
- Failed runs are retained as .failed-* for diagnostics.
- Retention touches only completed timestamp directories.
- The destination must already exist.
- backup_path_must_be_mount=True prevents accidental writes to the Engine root
  filesystem if an NFS mount disappears.
- The legacy Export Domain is not used.
- The web console is read-only until backup and restore are validated.
