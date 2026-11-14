#!/bin/bash
set -euo pipefail

EXPECTED_RPM="ovirt-imageio-daemon-2.5.2-1.el9.x86_64"
BACKUP_ROOT="/root/ovirt-imageio-rangelist-backups"
STAMP="$(date +%Y%m%d_%H%M%S)"

actual="$(rpm -q ovirt-imageio-daemon 2>/dev/null || true)"
if [[ "$actual" != "$EXPECTED_RPM" ]]; then
  echo "ERROR: expected $EXPECTED_RPM, got: $actual" >&2
  exit 1
fi

MEASURE="$(python3 - <<'PY'
import ovirt_imageio._internal.measure as m
print(m.__file__)
PY
)"

if [[ ! -f "$MEASURE" ]]; then
  echo "ERROR: measure.py not found: $MEASURE" >&2
  exit 1
fi

mkdir -p "$BACKUP_ROOT/$STAMP"
cp -a "$MEASURE" "$BACKUP_ROOT/$STAMP/measure.py"
echo "$MEASURE" > "$BACKUP_ROOT/$STAMP/path"

python3 - "$MEASURE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

old = '''    def add(self, r):
        """
        Add a single range.
        """
        bisect.insort_left(self._ranges, r)
        self._ranges = _merged(self._ranges)
'''

new = '''    def add(self, r):
        """
        Add a single range.

        Keep the list normalized without re-merging every previously
        transferred range on every request.  The packaged implementation
        calls _merged(self._ranges) after each insertion, which becomes
        increasingly expensive for transfers with many non-contiguous
        requests.

        Find the insertion point and merge only the new range with adjacent
        overlapping/consecutive ranges.
        """
        i = bisect.bisect_left(self._ranges, r)

        start = r.start
        end = r.end
        left = i

        if i > 0 and self._ranges[i - 1].end >= start:
            left = i - 1
            start = min(start, self._ranges[i - 1].start)
            end = max(end, self._ranges[i - 1].end)

        right = i
        while right < len(self._ranges) and self._ranges[right].start <= end:
            end = max(end, self._ranges[right].end)
            right += 1

        self._ranges[left:right] = [Range(start, end)]
'''

if new in text:
    print("Patch already applied")
elif old not in text:
    raise SystemExit(
        "ERROR: expected RangeList.add implementation not found; "
        "refusing to modify unknown source"
    )
else:
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("Patched:", path)
PY

python3 -m py_compile "$MEASURE"

python3 - <<'PY'
from ovirt_imageio._internal.measure import Range, RangeList

r = RangeList()
for item in (
    Range(100, 200),
    Range(300, 400),
    Range(200, 300),
    Range(50, 75),
    Range(70, 110),
):
    r.add(item)

actual = [(x.start, x.end) for x in r._ranges]
expected = [(50, 400)]
assert actual == expected, (actual, expected)
print("RangeList sanity test: OK", actual)
PY

systemctl restart ovirt-imageio
systemctl is-active --quiet ovirt-imageio

echo
echo "PATCH APPLIED"
echo "measure.py: $MEASURE"
echo "backup:     $BACKUP_ROOT/$STAMP/measure.py"
echo "service:    $(systemctl is-active ovirt-imageio)"
echo
echo "Rollback:"
echo "  cp -a '$BACKUP_ROOT/$STAMP/measure.py' '$MEASURE'"
echo "  python3 -m py_compile '$MEASURE'"
echo "  systemctl restart ovirt-imageio"
