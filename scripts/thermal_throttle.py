#!/usr/bin/env python3
"""Adaptive thermal throttle for the cold-tier migrations.

Two escalating levers:
  1. ADAPTIVE RATE -- writes a recommended bandwidth cap to /tmp/bwlimit_kbps.
     coldmig.py reads it before each title. Sustained heat lowers it;
     sustained coolness raises it back toward BW_MAX.
  2. PAUSE/RESUME -- crossing PAUSE_C SIGSTOPs the transfer immediately;
     dropping below RESUME_C SIGCONTs it. Fast path; the rate adjustment is
     the slow self-correcting one.

Pausing is safe at any instant: coldmig verifies every copy before deleting
its source, and rsync runs with --partial so interrupted files resume.

Drives are discovered by scanning /dev/sd? each cycle, NOT hardcoded letters:
this box reshuffles letters across reboots (the Windows drive has been sdg,
then sdk, then sdi; the boot SSDs moved from sdi/sdj to sdk/sdl). Anything
letter-based silently monitors the wrong disk after a reboot.

Ideal HDD range is 35-45C; 50C+ sustained is where reliability suffers.
Defaults target that.

Usage: thermal_throttle.py [--pause 52] [--resume 47] [--hot 50] [--cool 46]
                           [--bw-min 5000] [--bw-max 40000] [--bw-start 20000]
"""
import glob
import os
import re
import subprocess
import sys
import time


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


PAUSE_C = arg("--pause", 52)
RESUME_C = arg("--resume", 47)
HOT_C = arg("--hot", 50)
COOL_C = arg("--cool", 46)
INTERVAL = arg("--interval", 30)
BW_MIN = arg("--bw-min", 5000)
BW_MAX = arg("--bw-max", 40000)
BW_START = arg("--bw-start", 20000)
BWFILE = "/tmp/bwlimit_kbps"
LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/temps.log"


def all_drives():
    return sorted(os.path.basename(p) for p in glob.glob("/dev/sd?"))


def io_counter(dev):
    try:
        with open("/proc/diskstats") as fh:
            for line in fh:
                f = line.split()
                if len(f) > 8 and f[2] == dev:
                    return int(f[3]) + int(f[7])
    except OSError:
        pass
    return None


_last_io = {}


def busy_drives():
    """Only drives doing real block I/O since the last cycle.

    Polling a drive is not free: smartctl issues ATA commands that reset the
    drive's idle timer WITHOUT registering in /proc/diskstats. This script
    previously globbed all twelve drives every 30 s, which made it the single
    biggest reason the cold tier never reached standby -- it kept resetting
    the countdown on drives it had no reason to watch. ('-n standby' does not
    save you: it only skips drives ALREADY asleep, so an awake drive is
    polled, its timer resets, and it can never fall asleep. Chicken-and-egg.)

    An idle drive generates no heat, so there is nothing to monitor anyway.
    First cycle polls everything to establish a baseline.
    """
    out = []
    first = not _last_io
    for d in all_drives():
        io = io_counter(d)
        if io is None:
            continue
        prev = _last_io.get(d)
        _last_io[d] = io
        if first or prev is None or io != prev:
            out.append(d)
    return out


def temp(dev):
    """Temperature, or None if the drive is asleep or unreadable.

    `-n standby` is essential, not cosmetic: a plain `smartctl -A` spins up a
    sleeping drive to answer, so polling every 30 s would silently defeat the
    cold-tier spindown (hddstandby=60) this box now relies on. With -n,
    smartctl exits 2 without touching a standby drive. A sleeping drive is
    also, by definition, not generating heat -- so skipping it is correct.
    """
    for args in (["-n", "standby", "-A", f"/dev/{dev}"],
                 ["-n", "standby", "-l", "scttempsts", f"/dev/{dev}"]):
        try:
            r = subprocess.run(["smartctl"] + args, capture_output=True,
                               text=True, timeout=30)
        except subprocess.SubprocessError:
            continue
        if "STANDBY" in r.stdout.upper() or "SLEEP" in r.stdout.upper():
            return None          # asleep: leave it that way
        out = r.stdout
        for line in out.splitlines():
            if re.search(r"Temperature_Celsius|Airflow_Temperature", line, re.I):
                p = line.split()
                if len(p) >= 10 and p[9].isdigit():
                    return int(p[9])
        m = re.search(r"Current Temperature:\s+(\d+)", out)
        if m:
            return int(m.group(1))
    return None


def transfer_pids():
    out = subprocess.run(["ps", "-eo", "pid,comm"], capture_output=True, text=True).stdout
    return [l.split()[0] for l in out.splitlines()[1:]
            if len(l.split(None, 1)) == 2 and l.split(None, 1)[1].strip() in ("rsync", "cp")]


def workers_running():
    return subprocess.run(["pgrep", "-f", "coldmig.py|per_season.py|rescue_windrive.py"],
                          capture_output=True).returncode == 0


def note(msg):
    print(msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


def main():
    bw = BW_START
    with open(BWFILE, "w") as fh:
        fh.write(str(bw))
    paused = False
    hot_streak = cool_streak = 0
    note(f"=== throttle start: bw={bw}KB/s [{BW_MIN}-{BW_MAX}] "
         f"pause>={PAUSE_C}C resume<={RESUME_C}C ===")

    while True:
        temps = {d: t for d in busy_drives() if (t := temp(d)) is not None}
        if not temps:
            time.sleep(INTERVAL)
            continue
        hot_dev = max(temps, key=temps.get)
        hottest = temps[hot_dev]
        stamp = time.strftime("%H:%M:%S")
        pids = transfer_pids()

        if not paused and hottest >= PAUSE_C and pids:
            for p in pids:
                subprocess.run(["kill", "-STOP", p])
            paused = True
            note(f"{stamp} PAUSE {hot_dev}={hottest}C >= {PAUSE_C}C")
        elif paused and hottest <= RESUME_C:
            for p in transfer_pids():
                subprocess.run(["kill", "-CONT", p])
            paused = False
            note(f"{stamp} RESUME {hot_dev}={hottest}C <= {RESUME_C}C")

        if hottest >= HOT_C:
            hot_streak, cool_streak = hot_streak + 1, 0
        elif hottest <= COOL_C:
            cool_streak, hot_streak = cool_streak + 1, 0
        else:
            hot_streak = cool_streak = 0

        old = bw
        if hot_streak >= 3:            # ~90s sustained heat
            bw = max(BW_MIN, int(bw * 0.7))
            hot_streak = 0
        elif cool_streak >= 20:        # ~10 min sustained cool
            bw = min(BW_MAX, int(bw * 1.25))
            cool_streak = 0
        if bw != old:
            with open(BWFILE, "w") as fh:
                fh.write(str(bw))
            note(f"{stamp} RATE {old} -> {bw} KB/s ({hot_dev}={hottest}C)")

        note(f"{stamp} {'paused' if paused else 'run'} bw={bw} max={hot_dev}:{hottest}C  " +
             " ".join(f"{d}={t}" for d, t in sorted(temps.items())))

        if not workers_running() and not paused:
            for p in transfer_pids():
                subprocess.run(["kill", "-CONT", p])
            note(f"{stamp} no workers running -- throttle exiting")
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
