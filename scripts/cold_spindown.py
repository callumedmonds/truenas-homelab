#!/usr/bin/env python3
"""Enable idle spindown on the cold-tier drives.

Cold drives hold rarely-watched, re-downloadable media and sit idle almost
all the time, so spinning them down saves roughly 5-8 W each. The trade-off
is a few seconds of spin-up latency the first time Plex reads a cold title,
plus start/stop cycles -- which is why the timer is generous (60 min) rather
than aggressive.

Drives are matched by SERIAL, never by letter. This box reshuffles letters
across reboots; the old Windows drive alone has appeared as sdg, sdk, sdi and
sdh, and the boot SSDs have occupied those same letters at different times.

Deliberately NOT touched:
  * Cloud36 members -- the hot pool, read constantly by Plex.
  * boot-pool SSDs -- spindown on the boot device is asking for trouble, and
    SSDs draw little idle power anyway.

Note the weekly Sunday-00:00 scrub still wakes every cold drive for a full
surface read. For single-disk pools a scrub can detect bit rot but cannot
repair it (no parity), so monthly is arguably the better cadence for cold.

Usage: cold_spindown.py [--minutes 60] [--apply]
"""
import json
import subprocess
import sys

COLD_SERIALS = {
    "SERIAL0001": "cold01",
    "SERIAL0002": "cold02",
    "SERIAL0003": "cold03",
}
MINUTES = sys.argv[sys.argv.index("--minutes") + 1] if "--minutes" in sys.argv else "60"
APPLY = "--apply" in sys.argv


def midclt(*args):
    r = subprocess.run(["midclt", "call"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return json.loads(r.stdout) if r.stdout.strip() else None


disks = midclt("disk.query")
found = {}
for d in disks:
    if d["serial"] in COLD_SERIALS:
        found[COLD_SERIALS[d["serial"]]] = d

for pool in sorted(COLD_SERIALS.values()):
    d = found.get(pool)
    if not d:
        print(f"{pool:<8} NOT FOUND (drive absent?)")
        continue
    print(f"{pool:<8} {d['name']:<5} serial={d['serial']:<18} "
          f"standby={d.get('hddstandby')} apm={d.get('advpowermgmt')}")

if not APPLY:
    print(f"\n(dry run -- pass --apply to set hddstandby={MINUTES} minutes)")
    sys.exit(0)

for pool, d in sorted(found.items()):
    try:
        midclt("disk.update", d["identifier"], json.dumps({"hddstandby": MINUTES}))
        print(f"{pool:<8} {d['name']}: hddstandby -> {MINUTES} min")
    except RuntimeError as e:
        print(f"{pool:<8} {d['name']}: FAILED {e}")

print("\nverifying:")
for d in midclt("disk.query"):
    if d["serial"] in COLD_SERIALS:
        print(f"  {COLD_SERIALS[d['serial']]:<8} {d['name']:<5} "
              f"standby={d.get('hddstandby')}")
