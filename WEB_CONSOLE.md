# oVirt Backup Console

Version 0.1 of the web console is intentionally read-only while the native
backup and restore workflows are being validated.

## Architecture

- Backend: standard Python 3 HTTP server; no pip-only web framework.
- Bind address: 127.0.0.1:8765.
- Frontend: static HTML, CSS and JavaScript.
- Publication: Apache reverse proxy at /ovirt-backup/.
- API authentication: bearer token in /etc/ovirt-backup/web.token.
- Data sources: official oVirt Python SDK, native backup manifests, native
  backup log, systemd state, local filesystem statistics.

The backend never writes to the oVirt Engine database and does not use the
deprecated Export Domain workflow.

## Read-only API

- GET /api/overview
- GET /api/health
- GET /api/vms
- GET /api/backups
- GET /api/logs
- GET /api/settings
- GET /api/scheduler
- GET /api/capabilities

When /etc/ovirt-backup/web.token exists and is non-empty, every API request
requires the HTTP header Authorization: Bearer TOKEN. The browser UI stores
the token only in sessionStorage.

## Target RPM layout

/etc/ovirt-backup/backup.cfg
/etc/ovirt-backup/web.token
/usr/libexec/ovirt-backup/backup_native.py
/usr/libexec/ovirt-backup/ovirt_backup_web.py
/usr/share/ovirt-backup/web/index.html
/var/lib/ovirt-backup/
/var/log/ovirt-backup/
/usr/lib/systemd/system/ovirt-backup.service
/usr/lib/systemd/system/ovirt-backup.timer
/usr/lib/systemd/system/ovirt-backup-web.service

The RPM does not enable the backup timer automatically. Enabling scheduled
backups remains an explicit administrator action.

## Planned write mode

Write actions will be enabled only after native backup and restore validation.
Planned actions include dry-run, backup now, cancel after current VM, scheduler
editing, safe settings editing, test restore to a new VM, and Engine
engine-backup monitoring.
