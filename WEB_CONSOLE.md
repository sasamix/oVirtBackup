# oVirt Backup Console

The native web console now has a validated configuration mode. Real backup,
restore and scheduler write actions remain disabled until the configuration UI
has been validated on the target installation.

## Architecture

- Backend: standard Python 3 HTTP server; no pip-only web framework.
- Bind address: 127.0.0.1:8765.
- Frontend: static HTML, CSS and JavaScript.
- Publication: Apache reverse proxy at /ovirt-backup/.
- API authentication: bearer token in /etc/ovirt-backup/web.token.
- Canonical native configuration:
  /etc/ovirt-backup/config/backup.cfg.
- Configuration history:
  /var/lib/ovirt-backup/config-backups/.
- Data sources: official oVirt Python SDK, native backup manifests, native
  backup/restore logs, systemd state and local filesystem statistics.

The backend runs as the unprivileged `ovirt-backup` user. Only
`/etc/ovirt-backup/config/` is writable inside the systemd sandbox. The
parent directory and `web.token` are not writable by the console.

## API

Read operations:

- GET /api/overview
- GET /api/health
- GET /api/vms
- GET /api/backups
- GET /api/logs?kind=backup|restore
- GET /api/settings
- GET /api/scheduler
- GET /api/capabilities
- GET /api/operations/current
- GET /api/restore-options

Validated write operations currently enabled:

- POST /api/settings/validate
- POST /api/settings
- POST /api/settings/retention-preview
- POST /api/actions/dry-run
- POST /api/actions/backup
- POST /api/actions/restore-preflight
- POST /api/actions/restore

Manual native backup and restore actions are enabled. Scheduler mutation
remains intentionally disabled.

When /etc/ovirt-backup/web.token exists and is non-empty, every API request
requires the HTTP header `Authorization: Bearer TOKEN`. The browser UI stores
the token only in `sessionStorage`.

## Configuration safety

The settings API uses a fixed schema and rejects unknown keys. Numeric values
have bounds and ImageTransfer timeout policy is limited to the oVirt API values
`legacy`, `pause` and `cancel`.

VM selection must use exactly one mode:

- explicit `vm_names`;
- all VMs except `vm_names_skip`;
- `vm_tag`;
- `all_vms=True`.

The Engine password is never returned to the browser. Submitting an empty
password preserves the stored value.

Before saving, the generated INI is parsed and validated. The previous live
configuration is copied to `/var/lib/ovirt-backup/config-backups/`, then the
new file is fsynced and atomically renamed into place.

The Validate action checks:

- local schema and cross-field rules;
- Engine API authentication;
- selected VM existence;
- CA file;
- backup repository existence, mount state, writability and free space;
- configured ImageIO logical network;
- default restore storage domain;
- default restore cluster.

## Retention preview

The Settings page can evaluate the configured age/count retention rules against
completed native backup directories. The preview reports KEEP/DELETE decisions
but never deletes files. The algorithm follows backup_native.py order: age
filter first, then count limit.

## VM inventory and dry-run

The Virtual Machines page shows each VM's disks and storage domains. Selected
VMs can be copied into the Settings form or passed to the native backup script
with `--dry-run --vm-names ...`.

Dry-run refuses to start while the native operation lock is busy and never
starts an Engine backup.

## Self-test

Before starting the web service after an update, run:

```bash
/usr/bin/python3 /usr/libexec/ovirt-backup/ovirt_backup_web.py --self-test
```

The self-test uses a temporary directory only. It verifies schema coercion,
blank-password preservation, atomic config replacement/config history and
read-only retention preview. It does not connect to Engine and does not touch
the live configuration or backup repository.

## Manual operations

The web console starts real native backup/restore work through
`ovirt_backup_runner.py`. Each operation has a state JSON and a dedicated log
under `/var/lib/ovirt-backup/web-operations/`.

The runner invokes the existing `backup_native.py` or `restore_native.py`;
it does not implement a second backup engine. Native flock serialization remains
authoritative.

A manual backup is refused while `dry_run=True` in the saved configuration.
Restore offers a separate preflight action and requires an explicit confirmation
before execute. Restore still creates a new VM, never starts it automatically,
and uses the existing `none` / `isolated` / `original` network rules.

The web systemd unit uses `KillMode=process` so restarting only the web console
does not terminate a runner that already launched a native operation. Operation
state remains available after the console comes back.

## Target RPM layout

```text
/etc/ovirt-backup/config/backup.cfg
/etc/ovirt-backup/web.token
/usr/libexec/ovirt-backup/backup_native.py
/usr/libexec/ovirt-backup/restore_native.py
/usr/libexec/ovirt-backup/ovirt_backup_web.py
/usr/libexec/ovirt-backup/ovirt_backup_runner.py
/usr/share/ovirt-backup/web/index.html
/var/lib/ovirt-backup/
/var/lib/ovirt-backup/config-backups/
/var/lib/ovirt-backup/web-operations/
/var/log/ovirt-backup/
/usr/lib/systemd/system/ovirt-backup.service
/usr/lib/systemd/system/ovirt-backup.timer
/usr/lib/systemd/system/ovirt-backup-web.service
```

The RPM does not enable the backup timer automatically. Enabling or replacing
the existing legacy cron remains a separate final migration step.


## Migration guard

During coexistence with the legacy `pioner` cron, the console detects active
legacy `backup.py` processes. Real native Backup/Restore actions are refused
while a legacy backup is active, preventing the I/O contention observed during
migration tests. Settings, inventory, retention preview and native dry-run stay
available.

This guard does not modify, signal or stop the legacy process and requires no
change to branch `pioner` or its cron.


## Browser localization and operation UX

The static console chooses Russian when the browser's primary preferred language
(`navigator.languages[0]` / `navigator.language`) is `ru` or a `ru-*`
locale. Other browsers use English. Technical identifiers, VM names, storage
domain names, paths, IDs and native logs are not rewritten.

The top header is fixed while the page scrolls.

Real manual backup and restore actions use one confirmation before launch.
After the operation is accepted, the console switches to Dashboard and shows
the persistent operation state/log instead of displaying a second modal
"operation started" alert.
