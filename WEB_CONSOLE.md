# oVirt Backup Console

The native web console has validated configuration, inventory, retention
preview, dry-run, manual backup, manual restore and scheduler configuration
workflows. The remaining migration action is the explicit cutover from the
legacy `pioner` cron to the native timer.

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
- Scheduler privilege separation: the web process connects to
  `/run/ovirt-backup/scheduler.sock`. A dedicated root controller can only
  validate/apply the native timer schedule and inspect legacy cron state.

The backend runs as the unprivileged `ovirt-backup` user. Only
`/etc/ovirt-backup/config/` is writable inside the systemd sandbox. The
parent directory and `web.token` are not writable by the console.

## Engine session

The web process opens an oVirt SDK connection on its first Engine request
and reuses it for overview, VM inventory, health, configuration checks and
restore options. A lock serializes Engine operations across HTTP request
threads. If the SDK or transport fails, the console discards the connection,
reconnects and retries the read operation once. The SDK can renew its SSO
token when Engine expires it. The web service closes the connection on
shutdown, including when stopped by systemd.

Changing the saved Engine URL, credentials or TLS settings replaces the
shared connection on the next Engine request. Validating a draft with
different connection settings uses a temporary connection so the active
session stays available. Manual backup and restore run in separate
processes with their own Engine connections.

## API

Read operations:

- GET /api/overview
- GET /api/health
- GET /api/vms
- GET /api/backups
- GET /api/logs?kind=backup|restore
- GET /api/settings
- GET /api/settings/options
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
- POST /api/scheduler/validate
- POST /api/scheduler

Manual native backup and restore actions are enabled. Scheduler mutation is
implemented separately from backup configuration so it can remain disabled
while the legacy cron is still present.

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

The settings form loads ImageIO networks, restore clusters and storage domains,
VMs, tags, and disk IDs from Engine. Saved values stay visible when they are
missing from the inventory. ImageIO can use the default Engine route, and
the restore storage domain can be selected at restore time.

Existing single-line settings outside the native form can be edited in the
unmanaged settings section. These values use the same atomic save and
configuration history. Sensitive settings stay hidden. Inherited and
multiline values are read-only.

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

The self-test uses a temporary directory and a simulated SDK connection.
It verifies schema coercion, blank-password preservation, atomic config
replacement/config history, read-only retention preview, connection reuse,
a single reconnect, draft connection cleanup and shutdown.

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
and uses the existing `none` / `isolated` /
`isolated-original-mac` / `original` network rules.

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
/var/lib/ovirt-backup/restores/
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


## Restore network modes and saved MAC addresses

The restore UI exposes four network modes:

- `none`: restore no NICs.
- `isolated`: restore saved interface/profile with an Engine-assigned MAC and
  force `linked=false`.
- `isolated-original-mac`: restore the saved interface/profile and the exact
  saved MAC address, but force `linked=false`. This is useful for a test or
  staged restore that must keep guest licensing, DHCP reservations or other
  MAC-bound identity without bringing the restored NIC onto the network.
- `original`: restore the saved MAC/profile and original link state for real
  disaster recovery. This mode still refuses to run while the source VM exists
  in the same Engine.

For `isolated-original-mac`, preflight scans VM NICs for conflicting saved MAC
addresses and resolves the MAC pool used by the target cluster. If a saved MAC
is already in use, restore is allowed only when that MAC pool has
`allow_duplicates=true`; otherwise preflight fails before disks are uploaded
or a VM is created. The restored VM is never auto-started.


## Console build identification

The header shows the console semantic version and, when running from a live Git
checkout, the short Git commit. This makes it possible to verify the exact code
currently served without opening the repository on the host.


## Scheduler configuration

The Scheduler page configures `ovirt-backup.timer` without granting root
privileges to the web process.

The unprivileged console talks over
`/run/ovirt-backup/scheduler.sock` to
`ovirt-backup-scheduler-control.service`. The controller validates a fixed
scheduler schema, writes:

```text
/etc/ovirt-backup/scheduler.json
/etc/systemd/system/ovirt-backup.timer.d/10-schedule.conf
```

and then runs the required `systemctl daemon-reload` and timer
enable/disable operation.

Supported settings are:

- enabled/disabled;
- start time in 24-hour `HH:MM`;
- one or more weekdays;
- IANA timezone, default `Asia/Almaty`;
- optional randomized delay from 0 to 3600 seconds.

The generated timer is persistent. `systemd-analyze calendar` validates the
exact calendar expression before it can be applied.

### Migration guard

The controller scans system/user cron files for the legacy `backup.py` entry
and also scans running processes. A schedule may be prepared and saved while
legacy cron is present as long as `enabled=false`.

The controller refuses to enable the native timer while either a legacy cron
entry or a running legacy `backup.py` process is detected. It never edits,
comments, removes, signals or stops legacy cron/processes.

This makes cutover an explicit final migration action: first disable the exact
legacy cron entry, wait for any old backup process to finish, then enable the
native timer from the Scheduler page.
