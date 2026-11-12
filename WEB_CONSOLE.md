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

Validated write operations currently enabled:

- POST /api/settings/validate
- POST /api/settings
- POST /api/settings/retention-preview
- POST /api/actions/dry-run

Real backup, restore and scheduler mutation endpoints are intentionally not
enabled yet.

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

## Target RPM layout

```text
/etc/ovirt-backup/config/backup.cfg
/etc/ovirt-backup/web.token
/usr/libexec/ovirt-backup/backup_native.py
/usr/libexec/ovirt-backup/restore_native.py
/usr/libexec/ovirt-backup/ovirt_backup_web.py
/usr/share/ovirt-backup/web/index.html
/var/lib/ovirt-backup/
/var/lib/ovirt-backup/config-backups/
/var/log/ovirt-backup/
/usr/lib/systemd/system/ovirt-backup.service
/usr/lib/systemd/system/ovirt-backup.timer
/usr/lib/systemd/system/ovirt-backup-web.service
```

The RPM does not enable the backup timer automatically. Enabling or replacing
the existing legacy cron remains a separate final migration step.
