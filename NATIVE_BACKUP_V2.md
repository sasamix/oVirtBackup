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


## Restore prototype

`restore_native.py` restores one completed FULL backup into a new VM.

Safety defaults:

- preflight-only unless `--execute` is supplied;
- target VM name must not already exist;
- disk and NIC entries are stripped from the saved OVF before VM creation;
- backup disks are recreated and uploaded through ImageTransfer;
- restored disks are attached after upload;
- no NICs are restored;
- the VM is never started automatically;
- restore state is recorded under `/var/lib/ovirt-backup/restores`;
- failed restore resources are kept for diagnosis rather than deleted automatically.

This allows a restore test beside the running source VM without duplicate MAC or
IP conflicts.
