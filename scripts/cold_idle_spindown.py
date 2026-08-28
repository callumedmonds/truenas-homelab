#!/usr/bin/env python3
"""Spin down idle cold-tier drives based on real block I/O.

WHY NOT JUST hddstandby / hdparm -S:
The drive's own standby timer never fires on this box. Setting hddstandby=60
via the middleware, and hdparm -S 12 (1 minute) directly, both left the drives
"active/idle" indefinitely with ZERO I/O in /proc/diskstats. Periodic
status/SMART polling resets the ATA idle timer without appearing as block I/O,
so the countdown never completes.

But a FORCED standby sticks: `hdparm -y` put cold01 to sleep and it stayed
asleep through 90 s of subsequent checks. Those same polling commands are
answered by the drive's electronics and do NOT spin it back up.

So: watch /proc/diskstats (real reads/writes, the thing that actually matters),
and when a cold drive has done nothing for IDLE_MINUTES, issue hdparm -y.
This is more accurate than an ATA timer anyway -- it keys on genuine data
access rather than any command that happens to touch the bus.

Drives are matched by SERIAL, never letter: letters reshuffle across reboots
on this box (cold01 has been sdh and sdj; the old Windows drive has been sdg,
sdk, sdi and sdh). Only cold-tier drives are ever touched -- never Cloud36,
never the boot SSDs.

Usage: cold_idle_spindown.py [--idle-minutes 30] [--interval 60] [--once]
"""
import json
import os
import subprocess
import sys
import time

COLD_SERIALS = {
    "SERIAL0001": "cold01",
    "SERIAL0002": "cold02",
    "SERIAL0003": "cold03",
    "SERIAL0004": "cold04",
}


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


IDLE_MINUTES = arg("--idle-minutes", 30)
INTERVAL = arg("--interval", 60)
ONCE = "--once" in sys.argv
LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/spindown.log"


def note(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def cold_devices():
    """serial -> device name, resolved fresh each cycle."""
    try:
        disks = json.loads(subprocess.run(["midclt", "call", "disk.query"],
                                          capture_output=True, text=True).stdout)
    except (json.JSONDecodeError, OSError):
        return {}
    return {d["name"]: COLD_SERIALS[d["serial"]]
            for d in disks if d["serial"] in COLD_SERIALS}


def io_counter(dev):
    """completed reads+writes for dev, or None. SMART/ATA polling does not
    appear here -- which is exactly why we key on it."""
    try:
        with open("/proc/diskstats") as fh:
            for line in fh:
                f = line.split()
                if len(f) > 8 and f[2] == dev:
                    return int(f[3]) + int(f[7])
    except OSError:
        pass
    return None


def power_state(dev):
    out = subprocess.run(["hdparm", "-C", f"/dev/{dev}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "drive state" in line:
            return line.split(":")[-1].strip()
    return "unknown"


def main():
    last = {}          # dev -> (io_counter, timestamp_of_last_change)
    asleep = set()
    note(f"=== cold idle spindown started: idle>={IDLE_MINUTES}min, "
         f"poll={INTERVAL}s, drives={sorted(set(COLD_SERIALS.values()))} ===")
    while True:
        for dev, pool in sorted(cold_devices().items()):
            io = io_counter(dev)
            if io is None:
                continue
            now = time.time()
            prev_io, since = last.get(dev, (io, now))
            if io != prev_io:
                last[dev] = (io, now)
                if dev in asleep:
                    asleep.discard(dev)
                    note(f"{pool} ({dev}) woke -- real I/O")
                continue
            last[dev] = (prev_io, since)
            idle_min = (now - since) / 60
            if idle_min < IDLE_MINUTES or dev in asleep:
                continue
            state = power_state(dev)
            if state.startswith("standby") or state.startswith("sleeping"):
                asleep.add(dev)
                continue
            r = subprocess.run(["hdparm", "-y", f"/dev/{dev}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                asleep.add(dev)
                note(f"{pool} ({dev}) idle {idle_min:.0f} min -> standby")
            else:
                note(f"{pool} ({dev}) spindown FAILED: "
                     f"{(r.stderr or '').strip()[:120]}")
        if ONCE:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
