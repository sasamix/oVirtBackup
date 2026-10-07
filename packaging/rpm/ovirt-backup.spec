Name:           ovirt-backup-console
Version:        0.1.0
Release:        0.1%{?dist}
Summary:        Native oVirt VM backup tool and local web console
License:        GPL-3.0-or-later
URL:            https://github.com/sasamix/oVirtBackup
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

Requires:       python3
Requires:       python3-ovirt-engine-sdk4
Requires:       ovirt-imageio-client
Requires:       qemu-img
Requires:       httpd
Requires:       systemd

%description
Native online full backups for oVirt virtual machines using the VM Backup API
and ImageTransfer, plus a lightweight local web console. The implementation
does not use the deprecated oVirt Export Domain workflow.

%prep
%autosetup -n %{name}-%{version}

%build
# Pure Python and static web assets.

%install
install -Dpm 0755 backup_native.py \
    %{buildroot}%{_libexecdir}/ovirt-backup/backup_native.py
install -Dpm 0755 web/ovirt_backup_web.py \
    %{buildroot}%{_libexecdir}/ovirt-backup/ovirt_backup_web.py
install -Dpm 0644 web/static/index.html \
    %{buildroot}%{_datadir}/ovirt-backup/web/index.html

install -Dpm 0640 config_native_example.cfg \
    %{buildroot}%{_sysconfdir}/ovirt-backup/backup.cfg

install -Dpm 0644 packaging/systemd/ovirt-backup.service \
    %{buildroot}%{_unitdir}/ovirt-backup.service
install -Dpm 0644 packaging/systemd/ovirt-backup.timer \
    %{buildroot}%{_unitdir}/ovirt-backup.timer
install -Dpm 0644 packaging/systemd/ovirt-backup-web.service \
    %{buildroot}%{_unitdir}/ovirt-backup-web.service

install -Dpm 0644 packaging/apache/ovirt-backup.conf \
    %{buildroot}%{_sysconfdir}/httpd/conf.d/ovirt-backup.conf
install -Dpm 0644 packaging/logrotate/ovirt-backup \
    %{buildroot}%{_sysconfdir}/logrotate.d/ovirt-backup

install -d %{buildroot}%{_localstatedir}/lib/ovirt-backup
install -d %{buildroot}%{_localstatedir}/log/ovirt-backup

%pre
getent group ovirt-backup >/dev/null || groupadd -r ovirt-backup
getent passwd ovirt-backup >/dev/null || \
    useradd -r -g ovirt-backup -d %{_localstatedir}/lib/ovirt-backup \
    -s /sbin/nologin -c "oVirt Backup" ovirt-backup
exit 0

%post
%systemd_post ovirt-backup-web.service
%systemd_post ovirt-backup.timer
if [ ! -s %{_sysconfdir}/ovirt-backup/web.token ]; then
    umask 027
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
        > %{_sysconfdir}/ovirt-backup/web.token
    chown root:ovirt-backup %{_sysconfdir}/ovirt-backup/web.token
    chmod 0640 %{_sysconfdir}/ovirt-backup/web.token
fi

%preun
%systemd_preun ovirt-backup-web.service
%systemd_preun ovirt-backup.timer

%postun
%systemd_postun_with_restart ovirt-backup-web.service
%systemd_postun ovirt-backup.timer

%files
%license LICENSE
%doc README.md NATIVE_BACKUP_V2.md WEB_CONSOLE.md
%{_libexecdir}/ovirt-backup/backup_native.py
%{_libexecdir}/ovirt-backup/ovirt_backup_web.py
%{_datadir}/ovirt-backup/web/index.html
%config(noreplace) %attr(0640,root,ovirt-backup) %{_sysconfdir}/ovirt-backup/backup.cfg
%ghost %attr(0640,root,ovirt-backup) %{_sysconfdir}/ovirt-backup/web.token
%config(noreplace) %{_sysconfdir}/httpd/conf.d/ovirt-backup.conf
%config(noreplace) %{_sysconfdir}/logrotate.d/ovirt-backup
%{_unitdir}/ovirt-backup.service
%{_unitdir}/ovirt-backup.timer
%{_unitdir}/ovirt-backup-web.service
%dir %attr(0750,ovirt-backup,ovirt-backup) %{_localstatedir}/lib/ovirt-backup
%dir %attr(0750,ovirt-backup,ovirt-backup) %{_localstatedir}/log/ovirt-backup

%changelog
* Thu Oct 08 2026 sasamix <asm@pioner.kz> - 0.1.0-0.1
- Initial native VM Backup API implementation and read-only web console.
