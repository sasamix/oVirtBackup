#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-0.1.0}"
TOPDIR="${TOPDIR:-$HOME/rpmbuild}"
NAME="ovirt-backup-console"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v rpmbuild >/dev/null || {
    echo "rpmbuild is required (install rpm-build)." >&2
    exit 1
}

mkdir -p "$TOPDIR"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
mkdir -p "$WORK/$NAME-$VERSION"

git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$WORK/$NAME-$VERSION"
tar -C "$WORK" -czf "$TOPDIR/SOURCES/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"
cp "$ROOT/packaging/rpm/ovirt-backup.spec" "$TOPDIR/SPECS/ovirt-backup.spec"

rpmbuild -ba     --define "_topdir $TOPDIR"     --define "version_override $VERSION"     "$TOPDIR/SPECS/ovirt-backup.spec"

echo
echo "Built packages:"
find "$TOPDIR/RPMS" "$TOPDIR/SRPMS" -type f \( -name '*.rpm' -o -name '*.src.rpm' \) -print
