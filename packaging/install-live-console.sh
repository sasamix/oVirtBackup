#!/bin/bash
# Install/update the oVirt Backup Console from the current native-backup-v2
# checkout. This script intentionally does NOT touch legacy cron or the native
# backup timer.
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "ERROR: run as root"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SOURCE_CONFIG="${SOURCE_CONFIG:-$REPO_DIR/config.native.cfg}"
CONFIG_DIR="/etc/ovirt-backup/config"
CONFIG="$CONFIG_DIR/backup.cfg"
TOKEN="/etc/ovirt-backup/web.token"
STATE_DIR="/var/lib/ovirt-backup"
CONFIG_BACKUPS="$STATE_DIR/config-backups"
OPERATIONS="$STATE_DIR/web-operations"
RESTORES="$STATE_DIR/restores"
LOG_DIR="/var/log/ovirt-backup"
SERVICE="/etc/systemd/system/ovirt-backup-web.service"
NATIVE_SERVICE="/etc/systemd/system/ovirt-backup.service"
NATIVE_TIMER="/etc/systemd/system/ovirt-backup.timer"
SCHEDULER_CONTROL="/etc/systemd/system/ovirt-backup-scheduler-control.service"
SCHEDULER_SOCKET="/run/ovirt-backup/scheduler.sock"
APACHE="/etc/httpd/conf.d/ovirt-backup.conf"

echo "=== oVirt Backup Console live installer ==="
echo "Repository: $REPO_DIR"
echo "Source config: $SOURCE_CONFIG"
echo "Canonical config: $CONFIG"
echo

for file in     "$REPO_DIR/backup_native.py"     "$REPO_DIR/restore_native.py"     "$REPO_DIR/web/ovirt_backup_web.py"     "$REPO_DIR/web/ovirt_backup_runner.py"     "$REPO_DIR/web/ovirt_backup_scheduler.py"     "$REPO_DIR/web/ovirt_backup_scheduler.py"     "$REPO_DIR/web/static/index.html"     "$REPO_DIR/packaging/systemd/ovirt-backup.timer"
do
    if [ ! -f "$file" ]; then
        echo "ERROR: missing $file"
        exit 1
    fi
done

if [ ! -f "$SOURCE_CONFIG" ] && [ ! -f "$CONFIG" ]; then
    echo "ERROR: neither source nor canonical config exists"
    exit 1
fi

if ! getent group ovirt-backup >/dev/null; then
    groupadd -r ovirt-backup
fi
if ! getent passwd ovirt-backup >/dev/null; then
    useradd -r -g ovirt-backup -d "$STATE_DIR" -s /sbin/nologin         -c "oVirt Backup" ovirt-backup
fi

echo "=== Python syntax ==="
/usr/bin/python3 -m py_compile     "$REPO_DIR/backup_native.py"     "$REPO_DIR/restore_native.py"     "$REPO_DIR/web/ovirt_backup_web.py"     "$REPO_DIR/web/ovirt_backup_runner.py"
echo "Python syntax: OK"

echo
echo "=== Console self-test ==="
runuser -u ovirt-backup -- /usr/bin/python3     "$REPO_DIR/web/ovirt_backup_web.py" --self-test

install -d -m 0750 -o root -g ovirt-backup /etc/ovirt-backup
install -d -m 0750 -o ovirt-backup -g ovirt-backup "$CONFIG_DIR"
install -d -m 0750 -o ovirt-backup -g ovirt-backup     "$STATE_DIR" "$CONFIG_BACKUPS" "$OPERATIONS" "$RESTORES" "$LOG_DIR"

if [ ! -f "$CONFIG" ]; then
    echo
    echo "=== Installing canonical config from tested native config ==="
    install -m 0640 -o ovirt-backup -g ovirt-backup         "$SOURCE_CONFIG" "$CONFIG"
else
    echo
    echo "Canonical config already exists; keeping it unchanged."
    chown ovirt-backup:ovirt-backup "$CONFIG"
    chmod 0640 "$CONFIG"
fi

if [ ! -s "$TOKEN" ]; then
    echo "Generating web API token."
    umask 027
    /usr/bin/python3 - <<'PY' > "$TOKEN"
import secrets
print(secrets.token_urlsafe(32))
PY
    chown root:ovirt-backup "$TOKEN"
    chmod 0640 "$TOKEN"
else
    chown root:ovirt-backup "$TOKEN"
    chmod 0640 "$TOKEN"
fi

echo
echo "=== Runner self-test ==="
RUNNER_TEST_DIR="$(mktemp -d /tmp/ovirt-backup-runner-test.XXXXXX)"
chown ovirt-backup:ovirt-backup "$RUNNER_TEST_DIR"
runuser -u ovirt-backup -- /usr/bin/python3     "$REPO_DIR/web/ovirt_backup_runner.py"     --state "$RUNNER_TEST_DIR/state.json"     --log "$RUNNER_TEST_DIR/run.log"     -- /bin/true

/usr/bin/python3 - "$RUNNER_TEST_DIR/state.json" <<'PY'
import json
import sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
assert state["status"] == "complete", state
assert state["returncode"] == 0, state
print("Runner self-test: OK")
PY
rm -rf "$RUNNER_TEST_DIR"

echo
echo "=== Scheduler controller self-test ==="
runuser -u ovirt-backup -- /usr/bin/python3 \
    "$REPO_DIR/web/ovirt_backup_scheduler.py" self-test

cat > "$NATIVE_SERVICE" <<EOF
[Unit]
Description=oVirt Native VM Backup (live checkout)
After=network-online.target remote-fs.target
Wants=network-online.target
ConditionPathExists=$CONFIG

[Service]
Type=oneshot
User=ovirt-backup
Group=ovirt-backup
ExecStart=/usr/bin/python3 $REPO_DIR/backup_native.py -c $CONFIG
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadOnlyPaths=$CONFIG $REPO_DIR
ReadWritePaths=$STATE_DIR $LOG_DIR
EOF

install -m 0644 "$REPO_DIR/packaging/systemd/ovirt-backup.timer" "$NATIVE_TIMER"

cat > "$SCHEDULER_CONTROL" <<EOF
[Unit]
Description=oVirt Backup Scheduler Controller (live checkout)
After=local-fs.target
Before=ovirt-backup-web.service

[Service]
Type=simple
User=root
Group=ovirt-backup
ExecStart=/usr/bin/python3 $REPO_DIR/web/ovirt_backup_scheduler.py serve --socket $SCHEDULER_SOCKET
Restart=on-failure
RestartSec=2
RuntimeDirectory=ovirt-backup
RuntimeDirectoryMode=0750
UMask=0007
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=$REPO_DIR
ReadWritePaths=/etc/ovirt-backup /etc/systemd/system /run/ovirt-backup

[Install]
WantedBy=multi-user.target
EOF

cat > "$SERVICE" <<EOF
[Unit]
Description=oVirt Backup Console (live checkout)
After=network-online.target ovirt-backup-scheduler-control.service
Wants=network-online.target ovirt-backup-scheduler-control.service

[Service]
Type=simple
User=ovirt-backup
Group=ovirt-backup
ExecStart=/usr/bin/python3 $REPO_DIR/web/ovirt_backup_web.py --config $CONFIG --static-dir $REPO_DIR/web/static --token-file $TOKEN --backup-script $REPO_DIR/backup_native.py --restore-script $REPO_DIR/restore_native.py --runner-script $REPO_DIR/web/ovirt_backup_runner.py --config-backup-dir $CONFIG_BACKUPS --operation-dir $OPERATIONS --operation-lock $STATE_DIR/operation.lock --scheduler-socket $SCHEDULER_SOCKET --listen 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=3
KillMode=process
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadOnlyPaths=$TOKEN $REPO_DIR
ReadWritePaths=$CONFIG_DIR $STATE_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# The scheduler controller is safe to enable now: it does not run backups.
# The native backup timer itself is deliberately left exactly as-is until cutover.
systemctl enable ovirt-backup-scheduler-control.service
systemctl restart ovirt-backup-scheduler-control.service

for attempt in $(seq 1 20); do
    [ -S "$SCHEDULER_SOCKET" ] && break
    sleep 0.1
done
if [ ! -S "$SCHEDULER_SOCKET" ]; then
    echo "ERROR: scheduler controller socket did not appear: $SCHEDULER_SOCKET"
    systemctl status ovirt-backup-scheduler-control.service --no-pager --full || true
    journalctl -u ovirt-backup-scheduler-control.service -n 120 --no-pager || true
    exit 1
fi

systemctl enable ovirt-backup-web.service
systemctl restart ovirt-backup-web.service

echo
echo "=== Scheduler controller ==="
systemctl status ovirt-backup-scheduler-control.service --no-pager --full || {
    journalctl -u ovirt-backup-scheduler-control.service -n 100 --no-pager
    exit 1
}

echo
echo "=== Web service ==="
systemctl status ovirt-backup-web.service --no-pager --full || {
    journalctl -u ovirt-backup-web.service -n 100 --no-pager
    exit 1
}

echo
echo "=== Local authenticated API ==="
API_TOKEN="$(cat "$TOKEN")"

API_READY=0
API_RESPONSE="$(mktemp /tmp/ovirt-backup-api.XXXXXX)"
trap 'rm -f "$API_RESPONSE"' EXIT

for attempt in $(seq 1 40); do
    if curl -fsS         -H "Authorization: Bearer $API_TOKEN"         http://127.0.0.1:8765/api/capabilities         > "$API_RESPONSE" 2>/dev/null
    then
        API_READY=1
        break
    fi

    if ! systemctl is-active --quiet ovirt-backup-web.service; then
        echo "ERROR: ovirt-backup-web.service stopped before API became ready"
        systemctl status ovirt-backup-web.service --no-pager --full || true
        journalctl -u ovirt-backup-web.service -n 120 --no-pager || true
        exit 1
    fi

    sleep 0.25
done

if [ "$API_READY" -ne 1 ]; then
    echo "ERROR: web service stayed active but API did not become ready"
    ss -ltnp 2>/dev/null | grep ':8765' || true
    journalctl -u ovirt-backup-web.service -n 120 --no-pager || true
    exit 1
fi

cat "$API_RESPONSE"
echo

curl -fsS     -H "Authorization: Bearer $API_TOKEN"     http://127.0.0.1:8765/api/settings >/dev/null

echo "Settings API: OK"
rm -f "$API_RESPONSE"
trap - EXIT

if command -v httpd >/dev/null 2>&1 && systemctl list-unit-files httpd.service >/dev/null 2>&1; then
    echo
    echo "=== Apache reverse proxy ==="
    APACHE_BACKUP=""
    if [ -f "$APACHE" ]; then
        APACHE_BACKUP="$APACHE.backup.$(date +%Y%m%d_%H%M%S)"
        cp -a "$APACHE" "$APACHE_BACKUP"
    fi
    install -m 0644 "$REPO_DIR/packaging/apache/ovirt-backup.conf" "$APACHE"

    if ! httpd -t; then
        echo "ERROR: Apache config test failed"
        if [ -n "$APACHE_BACKUP" ]; then
            mv -f "$APACHE_BACKUP" "$APACHE"
        else
            rm -f "$APACHE"
        fi
        exit 1
    fi

    systemctl reload httpd
    echo "Apache proxy: OK"
else
    echo
    echo "Apache/httpd not detected; local console remains on 127.0.0.1:8765."
fi

echo
echo "============================================================"
echo "INSTALL COMPLETE"
echo "============================================================"
echo "Console service: ovirt-backup-web.service"
echo "Config:          $CONFIG"
echo "Config history:  $CONFIG_BACKUPS"
echo "Operations:      $OPERATIONS"
echo "Restore states:  $RESTORES"
echo "Token file:      $TOKEN"
echo
echo "Legacy cron:     UNCHANGED"
echo "Native timer:    UNIT INSTALLED/UPDATED; ENABLE STATE UNCHANGED"
echo "Scheduler ctrl:  enabled (controller only; it never starts a backup by itself)"
echo
echo "If Apache is active, open: https://$(hostname -f)/ovirt-backup/"
echo "Use the token from: $TOKEN"
