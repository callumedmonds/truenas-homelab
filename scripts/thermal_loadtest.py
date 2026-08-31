#!/usr/bin/env python3
"""Bounded, read-only thermal load test for the drive bays.

WHY THIS EXISTS:
Idle temperatures say nothing about cooling. After the mid-wall fan swap the
drives read 42C at idle -- but they read low 40s at idle with the OLD fans too.
The failure mode we care about (sda climbing past 50C and driving the migration
throttle to its floor) only appears under sustained sequential read across every
Cloud36 member at once. So: reproduce exactly that, on purpose, for a bounded
window, and watch the curve.

Sequential raw reads are the harshest realistic thermal load on a HDD -- platters
at full speed, actuator streaming, controller at max throughput -- and harder
than the migration ever was, because the migration also spent time on metadata
and on the single cold-tier write target.

SAFETY:
  * dd reads /dev/sdX with of=/dev/null. Read-only at the block layer; it cannot
    modify the pool. Safe to run against imported, in-use pools.
  * iflag=direct bypasses the page cache, so a 20-minute test does not evict the
    working set.
  * Hard abort if any tested drive reaches --abort-c (default 55C), well under
    the 60C SCT limit that has already been tripped 117 times on sda.
  * Every dd is tracked by PID via Popen and killed explicitly. No pkill -f,
    which on this box has repeatedly matched the invoking shell's own command
    line and killed the ssh session.
  * --minutes is enforced by the parent loop AND by timeout(1) inside each dd,
    so an orphaned parent cannot leave drives streaming forever.

DEVICE SELECTION IS BY SERIAL, NEVER BY LETTER. Letters reshuffle across reboots
on this box (the old Windows drive has been sdg, sdk, sdi and sdh; the boot SSDs
moved from sdi/sdj to sdk/sdl). A letter-based test would eventually stress the
wrong disk -- or the dead one, which hangs the reader.

Usage:
  thermal_loadtest.py --minutes 20 [--abort-c 55] [--interval 30]
                      [--serials A,B,C]      # default: Cloud36 members + cold03
"""
import json
import os
import subprocess
import sys
import time

# Cloud36 RAIDZ2 members plus cold03. These live in the front bays behind the
# mid-wall and are the ones the fan swap was meant to fix. Deliberately EXCLUDES:
#   SERIAL0002  cold02 -- dead, DID_NO_CONNECT, reading it hangs the test
#   SERIAL0001 cold01 -- ageing 1TB laptop drive, runs 31C, nothing to learn
#   SERIAL0004    cold04 -- ditto, 33C
#   SSDSERIAL1.../SSDSERIAL2... boot SSDs -- not thermally interesting
TEST_SERIALS = {
    "SERIAL0005": "Cloud36 (IronWolf, the hot one)",
    "SERIAL0006": "Cloud36",
    "SERIAL0007": "Cloud36",
    "SERIAL0008": "Cloud36",
    "SERIAL0009": "Cloud36",
    "SERIAL0010": "Cloud36",
    "SERIAL0003": "cold03",
}


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


MINUTES = arg("--minutes", 20)
INTERVAL = arg("--interval", 30)
ABORT_C = arg("--abort-c", 55)
LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/loadtest.log"


def note(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def resolve():
    """serial -> device name, for the serials we intend to test."""
    wanted = set(sys.argv[sys.argv.index("--serials") + 1].split(",")) \
        if "--serials" in sys.argv else set(TEST_SERIALS)
    try:
        disks = json.loads(subprocess.run(["midclt", "call", "disk.query"],
                                          capture_output=True, text=True).stdout)
    except (json.JSONDecodeError, OSError):
        note("FATAL: could not query disks")
        sys.exit(1)
    return {d["name"]: d["serial"] for d in disks if d["serial"] in wanted}


def temp(dev):
    r = subprocess.run(["smartctl", "-A", f"/dev/{dev}"],
                       capture_output=True, text=True, timeout=30)
    for line in r.stdout.splitlines():
        if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
            p = line.split()
            if len(p) >= 10 and p[9].isdigit():
                return int(p[9])
    return None


def sectors_read(dev):
    try:
        with open("/proc/diskstats") as fh:
            for line in fh:
                f = line.split()
                if len(f) > 8 and f[2] == dev:
                    return int(f[5])          # sectors read (512B units)
    except OSError:
        pass
    return 0


def main():
    devs = resolve()
    if not devs:
        note("FATAL: no target drives resolved")
        sys.exit(1)

    note("=" * 70)
    note(f"LOAD TEST START: {MINUTES} min, abort at {ABORT_C}C, "
         f"{len(devs)} drives: {', '.join(sorted(devs))}")
    baseline = {d: temp(d) for d in devs}
    note("baseline (idle): " + "  ".join(f"{d}={baseline[d]}C" for d in sorted(devs)))

    start_sectors = {d: sectors_read(d) for d in devs}
    procs = {}
    for d in devs:
        # timeout(1) is a belt-and-braces stop: even if this parent is killed,
        # no dd outlives the requested window.
        procs[d] = subprocess.Popen(
            ["timeout", str(MINUTES * 60 + 60), "dd", f"if=/dev/{d}",
             "of=/dev/null", "bs=1M", "iflag=direct"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    note(f"started {len(procs)} readers: " +
         " ".join(f"{d}:pid={p.pid}" for d, p in sorted(procs.items())))

    peak = dict(baseline)
    deadline = time.time() + MINUTES * 60
    aborted = False
    try:
        while time.time() < deadline:
            time.sleep(INTERVAL)
            temps = {d: temp(d) for d in devs}
            for d, t in temps.items():
                if t is not None and (peak[d] is None or t > peak[d]):
                    peak[d] = t
            elapsed = int((time.time() - (deadline - MINUTES * 60)) / 60 * 10) / 10
            mbps = {d: (sectors_read(d) - start_sectors[d]) * 512 / 1e6 /
                    max(1, time.time() - (deadline - MINUTES * 60)) for d in devs}
            note(f"t+{elapsed:>4.1f}m  " +
                 "  ".join(f"{d}={temps[d]}C" for d in sorted(devs)) +
                 f"   agg={sum(mbps.values()):.0f}MB/s")
            hot = [(d, t) for d, t in temps.items() if t is not None and t >= ABORT_C]
            if hot:
                note(f"*** ABORT: {hot} reached {ABORT_C}C ***")
                aborted = True
                break
    finally:
        for d, p in procs.items():
            p.kill()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.SubprocessError:
                pass
        note("readers stopped")

    time.sleep(5)
    final = {d: temp(d) for d in devs}
    note("-" * 70)
    note("RESULT" + ("  (ABORTED ON HEAT)" if aborted else "  (completed)"))
    for d in sorted(devs):
        rise = (peak[d] - baseline[d]) if None not in (peak[d], baseline[d]) else "?"
        note(f"  {d:<5} {devs[d]:<16} idle={baseline[d]}C  peak={peak[d]}C  "
             f"rise=+{rise}C  now={final[d]}C   {TEST_SERIALS.get(devs[d], '')}")
    note("=" * 70)
    sys.exit(1 if aborted else 0)


if __name__ == "__main__":
    main()
