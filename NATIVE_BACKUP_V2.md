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


### NFS mount and bursty sparse-transfer observation (2026-11-15)

A later 10-second Engine sample during the same `VMBitrix` backup showed a
much lower average on `enp8s0`:

- average RX: about 5.5 MiB/s;
- average TX: about 5.5 MiB/s;
- a one-second burst reached about 29.6 MiB/s in each direction.

RX and TX remained almost identical, confirming that Engine receives ImageIO
payload and immediately writes it to the backup NFS target. The large
second-to-second variation is consistent with sparse/zero extent processing:
logical backup progress can advance while little or no payload is physically
transferred, then burst when an allocated extent is read and written.

The backup NFS mount was confirmed as:

`192.168.150.6:/volume1/ovirt-backup` mounted on `/mnt/ovirt-backup`
using NFSv4.1 over TCP with `rsize=131072`, `wsize=131072`,
`hard,timeo=600,retrans=2`, client address `192.168.150.47`.

Do not infer a fixed 5-6 MiB/s or 27 MiB/s network/storage ceiling from short
samples of this sparse FULL backup. Use a clean sequential NFS write benchmark
after the backup completes, plus final backup duration and actual allocated
backup size, to separate repository throughput from sparse-image behavior.


### Active qemu-nbd source path for VMBitrix (2026-11-15)

During the slow `VMBitrix` FULL backup, the active VDSM ImageTransfer process
on `srv00` was:

`/usr/bin/qemu-nbd --socket /run/vdsm/nbd/3d5f6732-e217-40ca-af77-4bae0a28d922.sock --persistent --shared=8 --export-name= --cache=none --aio=native --read-only json:{"driver":"raw","file":{"driver":"file","filename":"/rhev/data-center/mnt/192.168.150.5:_data/bd68a843-42c8-46ab-89bf-19d60a268401/images/3a10c741-698f-4d50-b871-83774ea77026/192eecbd-ed4f-44bb-9334-0fa093275b57"}}`

Therefore the actual source path is the storage-domain file mounted from
`192.168.150.5`, read by qemu-nbd with `cache=none`, `aio=native`,
`read-only`, and up to 8 shared NBD clients. The configured
`gluster-network` controls the ImageIO transfer route from `srv00` to the
Engine; it does not mean the source disk itself is read through Gluster.

Previous performance tests established that the underlying NFS path can be much
faster than qemu-nbd cold reads: raw physical NFS reads were about 397 MB/s,
while qemu-nbd `cache=none,aio=native` cold reads were about 54-56 MiB/s and
a repeated cache-warmed pass reached about 260.8 MiB/s. A
`cache=writeback,aio=threads` run measured about 153.2 MiB/s, but the A/B/A
sequence showed cache warming, not a proven cache-mode improvement.

The current VMBitrix run later sustained only roughly 16-20 MiB/s logical
progress, which is below the previously measured cold qemu-nbd level. Do not
blame 10 GbE or the backup NFS target before checking the source NFS mount and
the specific source volume/snapshot behavior.


### Source NFS mount parameters confirmed (2026-11-15)

The source storage domain used by the slow VMBitrix backup is mounted on
`srv00` as:

`192.168.150.5:/data -> /rhev/data-center/mnt/192.168.150.5:_data`

with:

- NFSv4.2 over TCP;
- `rsize=1048576`, `wsize=1048576` (1 MiB);
- `soft,nosharecache,timeo=100,retrans=3`;
- client address `192.168.150.1`.

Routing to `192.168.150.5` is directly via `gluster-network` using source
`192.168.150.1`.

Therefore the current 16-20 MiB/s VMBitrix backup slowdown is **not** explained
by a small NFS rsize/wsize or by the source traffic taking the wrong network.
Further diagnosis should focus on source-volume behavior and NFS/RPC latency or
retransmissions during the active qemu-nbd read.


### VMBitrix source allocation and cumulative NFS RPC statistics (2026-11-15)

The active source volume for the slow VMBitrix backup has:

- virtual/file size: 107374182400 bytes = 100 GiB;
- allocated blocks: 195058416 x 512 bytes, approximately 93.0 GiB;
- `du -h`: 94G.

Therefore this disk is almost fully allocated. Sparse/zero skipping can save only
a small fraction of the 100 GiB image, so the observed 16-20 MiB/s logical
progress is close to actual data-read throughput for much of this run.

Cumulative source NFS mountstats for `192.168.150.5:/data` showed:

- READ operations: 17,537,673;
- retransmitted READ operations: 0 in the per-op counters;
- cumulative READ RTT: 33,964,855 ms, about 1.94 ms/op;
- cumulative READ execute time: 37,124,941 ms, about 2.12 ms/op.

These lifetime averages do not indicate an obvious persistent NFS latency or
retransmission problem, but they are cumulative over the long-lived mount and
cannot diagnose the current slow interval. Use interval-based `nfsiostat` or
equivalent measurements during the active backup before attributing the
slowdown to source NFS latency.


### Interval NFS behavior during slow VMBitrix backup (2026-11-15)

A 10-second `nfsiostat 1 10` sample on `srv00` for the source mount
`192.168.150.5:/data` showed:

- read throughput mostly around 1-3 MiB/s with short bursts around 11-12 MiB/s;
- read ops generally around 23-34 ops/s;
- average read RTT typically about 0.3-1.2 ms;
- average execution typically about 0.4-1.4 ms;
- retransmissions: 0%;
- RPC backlog: 0.

This rules out an obvious source-NFS latency, retransmission, or server backlog
problem during the sampled interval. The source server is responding quickly,
but the client side is issuing relatively little read work. Combined with the
nearly fully allocated VMBitrix volume and the much faster historical raw NFS
and qemu-nbd tests, the next diagnostic target is ImageIO/qemu-nbd request
concurrency rather than NFS transport tuning.

Historical comparison retained from previous tests:

- RAW FULL backup download: about 75.93 GiB in 299.152 s, approximately 260 MiB/s;
- cold qemu-nbd `cache=none,aio=native`: about 54-56 MiB/s;
- cache-warmed repeat: about 260.8 MiB/s;
- current VMBitrix sustained logical progress: roughly 16-20 MiB/s.

Do not repeat NFS rsize/wsize, routing, or retransmission tuning unless new
evidence contradicts these measurements.


### ImageIO worker concurrency confirmed during slow VMBitrix backup (2026-11-15)

On the Engine host, the active backup Python process had four simultaneous
ESTABLISHED TCP connections to `srv00:54322`, plus one older CLOSE-WAIT
connection:

- four ESTABLISHED connections from `192.168.150.47` to
  `192.168.150.1:54322`;
- process: the active `backup_native.py` Python process;
- configured `imageio_max_workers=4`.

Therefore the slowdown is **not** caused by `imageio_max_workers=4` silently
collapsing to one HTTP connection. The next diagnostic target is the path
between the four ImageIO workers and qemu-nbd/NBD scheduling, plus destination
sparse-file write serialization or CPU limits.
