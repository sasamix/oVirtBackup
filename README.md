> **native-backup-v2:** This branch contains the native VM Backup API workflow. See [NATIVE_BACKUP_V2.md](NATIVE_BACKUP_V2.md) for the backup/restore design and [WEB_CONSOLE.md](WEB_CONSOLE.md) for the validated configuration console. The legacy stable workflow remains on branch `pioner`.

# oVirtBackup

## Native backup v2 status

The active `native-backup-v2` branch uses the official oVirt VM Backup API
and ImageTransfer. FULL disk payloads are stored as sparse RAW files on a normal
filesystem/NFS backup repository. Restore supports RAW and older qcow2 backup
images.

Validated workflows in this branch include:

- per-storage-domain ImageTransfer host selection;
- external NFS backup and restore;
- persistent native backup/restore logging and metrics;
- native operation locking;
- web configuration validation and atomic config history;
- VM/disk/storage inventory and native dry-run;
- retention preview;
- manual backup and restore through a persistent runner;
- restore modes with new or saved MAC addresses;
- browser-localized web UI;
- safe systemd scheduler configuration with a legacy-cron migration guard.

Canonical live configuration:

```text
/etc/ovirt-backup/config/backup.cfg
```

The stable legacy implementation remains unchanged on branch `pioner` as the
rollback path. See `NATIVE_BACKUP_V2.md` and `WEB_CONSOLE.md` for the current
native workflow.

### Live console update

For the migration installation used on HostedEngine, update the
`native-backup-v2` checkout and run:

```bash
./packaging/install-live-console.sh
```

The live installer updates the web console, native service/timer unit files and
the scheduler controller. It does **not** enable the native backup timer and it
does **not** modify the legacy cron.

---

> **native-backup-v2 branch:** the active implementation in this branch uses
> the official oVirt VM Backup API + ImageTransfer, stores FULL backup disks as
> sparse RAW, and restores with automatic per-storage-domain ImageTransfer host
> selection. See [NATIVE_BACKUP_V2.md](NATIVE_BACKUP_V2.md).
>
> The legacy snapshot/clone/Export Domain workflow documented below remains for
> compatibility and for the stable `pioner` rollback branch.

This is a tool, written in Python, to make **online** fullbackup's of a VM which runs in an oVirt environment.

**WARNING: This release has to be tested with oVirt 4.4. For oVirt 4.3 please take a look the the branches.**

## Requirements

It is necessary to install the oVirt Python-sdk.
In addition when running Pyhon < 2.7 you need you install argparse.

[http://www.ovirt.org/Python-sdk](http://www.ovirt.org/Python-sdk)

## Usage

Take a look at the usage text.

    backup.py -h

## Configuration

Take a look at the example "config_example.cfg"

Please avoid Cirillic symbols in the configuration otherwise you will get an exception see [#59](https://github.com/wefixit-AT/oVirtBackup/issues/59)

## Workflow

* Create a snapshot
* Clone the snapshot into a new VM
* Delete the snapshot
* Delete previous backups (if set)
* Export the VM to the NFS share
* Delete the VM

## Useful tips

### crontab

	00  20  *   *   *   /home/backup/oVirtBackup.git/backup.py -c /home/backup/oVirtBackup.git/config_webserver.cfg -d >> /var/log/oVirtBackup/webserver.log 

Logrotate: /etc/logrotate.d/oVirtBackup

	/var/log/oVirtBackup/* {
    	daily
    	rotate 14
    	compress
    	delaycompress
    	missingok
    	notifempty
	}
	
### Security

Set permissions to config.cfg only for the needed user (chmod 600 config.cfg).

## TODO's

* When the ovirtsdk supports exporting a snapshot directly to a domain, the step of a VM creation can be removed to save some disk space during backup

## Useful links:

* [http://www.ovirt.org/REST-Api](http://www.ovirt.org/REST-Api)
* [http://www.ovirt.org/Python-sdk](http://www.ovirt.org/Python-sdk)
* [http://www.ovirt.org/Testing/PythonApi](http://www.ovirt.org/Testing/PythonApi)
* [https://access.redhat.com/documentation/en-US/Red_Hat_Enterprise_Virtualization/3.1/html-single/Developer_Guide/files/ovirtsdk.infrastructure.brokers.html#VMSnapshot](https://access.redhat.com/documentation/en-US/Red_Hat_Enterprise_Virtualization/3.1/html-single/Developer_Guide/files/ovirtsdk.infrastructure.brokers.html#VMSnapshot)

## Running tests

Install [tox](http://tox.readthedocs.io/en/latest/index.html).

```sh
$ tox
```
