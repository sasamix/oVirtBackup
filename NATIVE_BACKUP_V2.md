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

When `imageio_network` is configured, native backup/restore routes the
ImageTransfer endpoint over that logical network while preserving the host
FQDN for TLS verification.

For live VM Backup downloads, Engine owns the VM backup host and the per-disk
backup URL. The native client therefore does **not** force a host from
`storage.address`; it follows the actual ImageTransfer host returned by
Engine and routes that host over `imageio_network`.

For restore uploads, the selected target storage domain is evaluated:
`storage.address` is matched to the IPv4 address of an UP oVirt host on
`imageio_network`. If there is no unique match (for example an external
Synology NFS server), host selection is left to Engine instead of guessing.

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


## ImageIO HTTP EXTENTS timeout

The ovirt-imageio HTTP backend currently defaults to a 60 second socket read
timeout. Its public `download()` / `upload()` API does not expose that
timeout, and large image EXTENTS requests can legitimately take longer before
data transfer begins.

Native backup/restore use `imageio_http_read_timeout` (default 300 seconds) to
override the HTTP backend read timeout only inside the native process. The
system ovirt-imageio package is not modified. The backup log reports metadata /
EXTENTS latency separately from actual data-transfer throughput.


## Confirmed ImageTransfer EXTENTS failure and fix (2026-11-15)

Observed failure on `VMBitrix` (100 GiB disk) while downloading a live VM
backup from `srv00.local.pioner.kz` over `gluster-network`.

Confirmed sequence:

1. Engine created the VM backup and reached `BackupPhase.READY`.
2. Engine selected `srv00` as the live-backup ImageTransfer host.
3. Direct ImageIO routing correctly used
   `srv00.local.pioner.kz:54322` / `192.168.150.1:54322`; proxy fallback
   remained disabled.
4. ImageIO started a `zero` EXTENTS request before data download.
5. The ImageTransfer ticket had Engine's default
   `inactivity_timeout=60`, while the EXTENTS calculation took longer than
   60 seconds.
6. Engine removed/cancelled the transfer while EXTENTS was still running.
   VDSM then stopped the corresponding `qemu-nbd` service.
7. ImageIO consequently logged:
   `ovirt_imageio._internal.nbd.ProtocolError: Server closed the connection`.
   This was a consequence of transfer removal, not an independent qemu-nbd
   crash.
8. The backup client then received
   `Server failed to perform the request, check logs`.

Important distinction: `imageio_http_read_timeout=300` was already working
and was not the cause. The failing timeout was the Engine ImageTransfer client
inactivity timeout.

Fix implemented in `backup_native.py`:

- new setting `imageio_inactivity_timeout`, default 300 seconds;
- pass it explicitly as `ImageTransfer.inactivity_timeout` when creating
  download transfers;
- keep `imageio_http_read_timeout=300` for long HTTP EXTENTS responses;
- do not enable ImageIO proxy fallback; direct `gluster-network` routing is
  preserved.

Validation after the fix:

- ImageTransfer log:
  `inactivity_timeout=300s timeout_policy=cancel`;
- EXTENTS completed successfully after approximately **201.5 seconds**;
- download then started normally at roughly 80-100 MiB/s;
- therefore a 60 second inactivity timeout is insufficient for this storage
  layout, and 300 seconds should not be reduced without new measurements.

### Failed-transfer cleanup

A second issue was confirmed during the same incident. On download failure the
old code called `ImageTransfer.cancel()` and immediately propagated the
exception. The VM-backup error handler then called `backup_service.finalize()`
while the ImageTransfer was still active, producing HTTP 409:

`Cannot stop VM backup. There is an active image transfer for VM backup`.

The cleanup path now waits after `cancel()` until the ImageTransfer either
reaches a finished phase or disappears before VM backup finalization proceeds.

A stale backup from the pre-fix run remained in `READY` and kept the VM disk
locked. After its failed ImageTransfer had reached `finished_failure`, manual
VM-backup finalization moved it through `REMOVING_SNAPSHOT` and finally to
`succeeded`, releasing the disk lock.

### Time correlation note

During diagnosis, Engine and `srv00` clocks were offset by about 32 seconds.
When correlating Engine and host logs, compare transfer IDs and event order, not
wall-clock timestamps alone.


### Throughput interpretation: logical progress vs physical network (2026-11-15)

During the fixed `VMBitrix` backup, the native progress logger later reported
about 44 MiB/s. A simultaneous `sar -n DEV 1 5` measurement on
`srv00.local.pioner.kz` showed only about 10.2 MiB/s RX and 10.1 MiB/s TX on
`gluster-network` on average.

This is expected for sparse RAW download: the progress callback advances by
logical image extents processed, while zero/hole extents can be skipped instead
of being transferred as physical payload. Therefore the displayed
`Download progress ... MiB/s` is a **logical virtual-image throughput**, not
a direct measurement of network bandwidth.

Do not diagnose a 44 MiB/s progress value as a 44 MiB/s network bottleneck
without simultaneous interface measurements. Use host and Engine interface
statistics to determine actual transport throughput and destination write rate.

The `vnet*` interface `%ifutil` values may be nonsensical because virtual
interfaces do not expose a meaningful physical link speed; use byte rates
(rxkB/s/txkB/s) and the physical/logical-network interfaces instead.


### Engine interface throughput observation (2026-11-15)

During the same `VMBitrix` backup, `sar -n DEV 1 5` on the Engine host showed
that `enp8s0` carried nearly symmetric traffic:

- average RX: about 27.0 MiB/s;
- average TX: about 26.9 MiB/s;
- observed peaks: roughly 47.5 MiB/s RX and 55.3 MiB/s TX.

This confirms that the Engine host is acting as a relay between two network
flows on the same interface: receiving ImageIO data from the oVirt host and
writing the sparse RAW backup to the remote `/mnt/ovirt-backup` target over
NFS.

Important correction: on a full-duplex 10 GbE link, simultaneous RX and TX on
the same NIC do **not** inherently halve throughput. The NIC can receive and
transmit concurrently. Therefore the observed ~27 MiB/s RX and ~27 MiB/s TX
does not by itself identify the NIC as the bottleneck.

Routing was confirmed:
- `192.168.150.1` (srv00 ImageIO path) uses `enp8s0` with source
  `192.168.150.47`;
- `/mnt/ovirt-backup` is NFSv4 from
  `192.168.150.6:/volume1/ovirt-backup`, also on the 192.168.150.0/24
  storage network.

The next performance diagnosis should separately measure:
1. sequential write throughput from Engine to the NFS backup target;
2. source read throughput from the oVirt/Gluster backing storage;
3. ImageIO/qemu-nbd behavior after EXTENTS.
Do not assume the shared Engine NIC is the bottleneck without these tests.


### Live log refresh behavior (console v0.5.2)

The Dashboard operation log is polled every 3 seconds. The browser must not
rewrite the log DOM when the returned text is unchanged, because assigning
`textContent` destroys an active text selection and makes copying long logs
difficult.

Console v0.5.2 therefore:

- updates a live log only when its text actually changed;
- defers a log DOM update while the user has an active selection inside it;
- catches up automatically on the next poll after the selection is released;
- auto-scrolls only when the viewer was already near the bottom, preserving a
  manually scrolled position.

The same update helper is used for the dedicated Logs page.
