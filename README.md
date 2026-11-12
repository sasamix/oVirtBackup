# oVirtBackup

oVirtBackup 0.5.0 uses the oVirt VM Backup API and ImageTransfer to create full
VM backups on a mounted filesystem. Disk images are stored as sparse RAW files.
Restore creates a new VM and also supports older qcow2 backups. The optional web
console provides configuration, inventory, backup, restore, logs and scheduling.

## Requirements

- An oVirt Engine and hosts that support the native VM Backup API and ImageTransfer.
- A Linux systemd host with Python 3.9+, the oVirt Python SDK 4.5+, the
  ovirt-imageio client and qemu-img. The RPM targets EL9-style packaging.
- A backup filesystem mounted on the machine that runs the backup, with space
  for the selected VMs and write access for the service account.
- An Engine account authorized for the intended operations and a trusted Engine
  CA. For remote web access, use an HTTPS reverse proxy.

## Install

Build the RPM on an EL9-compatible build host with `git`, `rpm-build` and
`systemd-rpm-macros` installed:

```bash
./packaging/build-rpm.sh
```

Install the resulting `ovirt-backup-console` RPM using your package manager.
For a checkout-based installation on an Engine host, run as root:

```bash
./packaging/install-live-console.sh
```

Both installation paths use `/etc/ovirt-backup/config/backup.cfg`.
A fresh install starts from `config_native_example.cfg` in dry-run mode.
Set your Engine URL, account, CA, VM selection and mounted backup path.
An existing native config is preserved. The checkout installer also accepts a
private source config via `SOURCE_CONFIG`.

After RPM installation, start the console and its controller:

```bash
systemctl enable --now ovirt-backup-web.service
```

The console listens on localhost port 8765 and authenticates API requests with
the token in `/etc/ovirt-backup/web.token`. For first-run and upgrade details,
see [WEB_CONSOLE.md](WEB_CONSOLE.md).

## First backup and migration

1. Validate configuration and run a native dry-run.
2. Run a manual full backup and test restore to a new VM before scheduling.
3. For a migration, disable the previous backup job and allow it to finish.
4. Set the local schedule and time zone in the console, then enable the timer.
   New configurations use UTC by default. Existing saved schedules are preserved.

Scheduled backups use the mounted backup path. The restore storage domain is
selected in the restore dialog and can be saved as a default.

See [NATIVE_BACKUP_V2.md](NATIVE_BACKUP_V2.md) for the backup format and restore
behavior. The [pioner branch](https://github.com/sasamix/oVirtBackup/tree/pioner)
contains the legacy workflow.
