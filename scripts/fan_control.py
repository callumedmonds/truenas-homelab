#!/usr/bin/env python3
"""Adaptive fan control for the X9DR3-LN4F+ in the SC847.

WHAT IS ACTUALLY CONTROLLABLE ON THIS BOX
-----------------------------------------
Measured 2026-08-31, not assumed. Only ZONE 0 responds:

    zone 0 (reg 0x10)  FAN3 = CPU1, FAN6 = CPU2, FAN5 = 2x rear 80mm on a
                       Y-splitter. Duty 0x00-0xff, near-linear in RPM.
    zone 1 (reg 0x11)  FANA/FANB = the two midwall 140mm Arctic P14s.
                       DOES NOTHING. Writing 0x00 (full off) left both
                       spinning at 1575 RPM, so they are 3-pin DC fans, not
                       PWM. Supermicro headers are PWM-only (pin 2 is
                       constant 12V), so there is no voltage-control
                       fallback -- they run flat out, always. Replacing them
                       with P14 PWM is the only way to control drive-bay
                       airflow. Zone 1 is still driven below so the curve
                       starts working the moment PWM fans are fitted.

The BMC must be in FULL mode (0x01) for manual duty to stick; any auto mode
overrides us. Re-asserted every cycle because it does not survive a BMC
reset.

WHY THE FLOOR IS 0x50 AND NOT LOWER
-----------------------------------
The BMC's own fan-fail failsafe is the binding constraint, not the fans.
Thresholds are lnc=600 / lcr=450 / lnr=300 RPM, and dropping under lcr makes
the BMC slam EVERY fan to 100% and log to the SEL. Observed directly:

    14:29:56  Fan FAN6  Lower Critical going low  Asserted    450 < 450 RPM
    14:30:03  Fan FAN6  Lower Critical going low  Deasserted  2100 > 450 RPM

Seven seconds from stall-detect to full blast. Measured duty -> RPM, with
FAN6 always the slowest and therefore the one that trips:

    duty   FAN3  FAN5  FAN6
    0x40    525   675   525   <-- under lnc, do not go here
    0x50    750   825   675   <-- floor, +75 RPM margin over lnc
    0x58    825   900   750
    0x60    900   975   825
    0x80   1200  1200  1125
    0xff   2175  2025  2100

A curve that dips below the floor does not merely run quiet -- it induces a
full-speed ramp, which is louder than never having throttled at all.

DRIVE TEMPERATURES AND THE SPINDOWN TRAP
----------------------------------------
Polling a drive is not free. smartctl issues ATA commands that reset the
drive's idle timer WITHOUT registering in /proc/diskstats. A naive fan
daemon polling all twelve drives every 30 s would silently guarantee the
cold tier never sleeps -- exactly the bug that made thermal_throttle.py the
dominant poller on this box. ('-n standby' does not save you: it only skips
drives ALREADY asleep, so an awake drive is polled, its timer resets, and it
can never fall asleep.)

So drive temps come only from drives that did real block I/O since the last
cycle. An idle drive generates no heat, so there is nothing to miss.

Cold-tier spindown is currently DISABLED after cold02 (a ~17,000-hour
Samsung HD502HJ) failed to spin back up on 2026-08-28 and took its pool with
it. This daemon is written to be safe if spindown is ever re-enabled.

FAILING SAFE
------------
On any exit -- clean, SIGTERM, or unhandled exception -- fans go to 0xff.
A daemon that dies holding a low duty would leave the box with no thermal
protection at all, which is far worse than a loud server. The cron watchdog
in fan-control-launch.sh restarts it; between death and restart the fans are
at full.

If any populated fan reports a non-ok state, the curve is abandoned and the
duty pinned at 0xff until it clears: a failed fan means the remaining ones
have to cover for it, and it also means the BMC is fighting us.

Usage: fan_control.py [--interval 30] [--min 0x58] [--once] [--dry-run]
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/fan-control.log"
STATE = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/fan-state.json"

ZONE_REGS = {0: "0x10", 1: "0x11"}
DUTY_MAX = 0xFF
DUTY_FLOOR = 0x58          # FAN6 ~750 RPM; 0x50 works but leaves less margin

# CPU temperature -> duty. Piecewise linear between these points.
# E5-2600 Tcase is ~75-80C; idle on this box is 33-41C.
CPU_CURVE = [(45, 0x58), (50, 0x70), (55, 0x90), (60, 0xB0), (65, 0xD0), (70, 0xFF)]

# Hottest busy drive -> minimum duty. The rear 80mm fans are the chassis
# extract path, so they still matter for drive temps even though the midwall
# fans (the actual bay airflow) are uncontrollable. Ideal HDD range is
# 35-45C; sda has 23 lifetime over-temperature events and a 64C max.
DRIVE_FLOOR = [(43, 0x80), (45, 0xA0), (47, 0xC0), (50, 0xFF)]

# Peripheral Temp sits at 48-51C normally on this board, so this only fires
# on a genuine airflow problem.
PERIPH_FLOOR = [(60, 0xB0), (65, 0xFF)]

# Ramp up instantly, step down only after this many consecutive cooler
# cycles. 10 x 30 s = 5 min, chosen against the thermal mass of a spinning
# 3.5" drive rather than the CPU: a drive takes minutes to settle, so a
# shorter streak makes the duty hunt up and down around DRIVE_FLOOR forever.
DOWN_STREAK = 10


def arg(name, default, cast=int):
    if name in sys.argv:
        v = sys.argv[sys.argv.index(name) + 1]
        return cast(v, 0) if cast is int else cast(v)
    return default


INTERVAL = arg("--interval", 30)
DUTY_MIN = arg("--min", DUTY_FLOOR)
ONCE = "--once" in sys.argv
DRY = "--dry-run" in sys.argv


def note(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def ipmi(*args):
    try:
        r = subprocess.run(["ipmitool"] + list(args), capture_output=True,
                           text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except subprocess.SubprocessError:
        return ""


def set_duty(zone, duty):
    if DRY:
        return True
    r = subprocess.run(["ipmitool", "raw", "0x30", "0x91", "0x5a", "0x03",
                        ZONE_REGS[zone], hex(duty)], capture_output=True)
    return r.returncode == 0


def assert_full_mode():
    """Manual duty only sticks in FULL mode, and mode resets with the BMC."""
    out = ipmi("raw", "0x30", "0x45", "0x00").strip()
    if out and int(out, 16) != 1:
        note("fan mode was %s -- setting FULL" % out)
        if not DRY:
            ipmi("raw", "0x30", "0x45", "0x01", "0x01")


def sensor_temps():
    """CPU/System/Peripheral from IPMI. Free -- never touches a drive."""
    out = ipmi("sdr", "type", "temperature")
    temps = {}
    for line in out.splitlines():
        m = re.match(r"\s*(CPU1 Temp|CPU2 Temp|System Temp|Peripheral Temp)\s*\|"
                     r".*\|\s*(\d+) degrees", line)
        if m:
            temps[m.group(1)] = int(m.group(2))
    return temps


def fan_rpms():
    """name -> (state, rpm|None) for populated headers only."""
    out = ipmi("sdr", "type", "fan")
    fans = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[0].startswith("FAN"):
            continue
        if "No Reading" in parts[4] or parts[2] == "ns":
            continue                      # empty header
        m = re.match(r"(\d+)", parts[4])
        fans[parts[0]] = (parts[2], int(m.group(1)) if m else None)
    return fans


_last_io = {}


def busy_drives():
    """Only drives doing real block I/O since the last cycle. See docstring.

    The FIRST cycle deliberately returns nothing. It only seeds the counters.
    Returning every drive on cycle 1 -- which is the obvious way to write
    this -- would spin up the entire cold tier on every daemon restart, and
    cron restarts this daemon. cold02 died on 2026-08-28 failing to spin back
    up from standby, so a routine restart must never be a wake-up event. One
    cycle (30 s) of no drive data at startup costs nothing: the CPU curve is
    already active and drives cannot heat meaningfully in that window.
    """
    out, first = [], not _last_io
    for path in sorted(glob.glob("/dev/sd?")):
        dev = os.path.basename(path)
        try:
            with open("/proc/diskstats") as fh:
                io = None
                for line in fh:
                    f = line.split()
                    if len(f) > 8 and f[2] == dev:
                        io = int(f[3]) + int(f[7])
                        break
        except OSError:
            continue
        if io is None:
            continue
        prev = _last_io.get(dev)
        _last_io[dev] = io
        if not first and prev is not None and io != prev:
            out.append(dev)
    return out


def drive_temp(dev):
    """None if asleep or unreadable -- never spins a drive up to ask."""
    for a in (["-n", "standby", "-A", "/dev/" + dev],
              ["-n", "standby", "-l", "scttempsts", "/dev/" + dev]):
        try:
            r = subprocess.run(["smartctl"] + a, capture_output=True,
                               text=True, timeout=30)
        except subprocess.SubprocessError:
            continue
        up = r.stdout.upper()
        if "STANDBY" in up or "SLEEP" in up:
            return None
        for line in r.stdout.splitlines():
            if re.search(r"Temperature_Celsius|Airflow_Temperature", line, re.I):
                p = line.split()
                if len(p) >= 10 and p[9].isdigit():
                    return int(p[9])
        m = re.search(r"Current Temperature:\s+(\d+)", r.stdout)
        if m:
            return int(m.group(1))
    return None


def interpolate(temp, curve):
    """Piecewise-linear lookup, clamped at both ends."""
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if temp <= t1:
            span = t1 - t0
            return int(d0 + (d1 - d0) * (temp - t0) / span) if span else d1
    return curve[-1][1]


def floor_for(value, table):
    duty = 0
    for threshold, d in table:
        if value is not None and value >= threshold:
            duty = max(duty, d)
    return duty


def target_duty(temps, drives_max):
    cpu = max([v for k, v in temps.items() if k.startswith("CPU")] or [0])
    duty = interpolate(cpu, CPU_CURVE)
    duty = max(duty, floor_for(drives_max, DRIVE_FLOOR))
    duty = max(duty, floor_for(temps.get("Peripheral Temp"), PERIPH_FLOOR))
    return max(DUTY_MIN, min(DUTY_MAX, duty)), cpu


_drive_seen = {}          # dev -> {"temp": int, "ts": float}


def write_state(duty, cpu, temps, fans, dtemps):
    """Publish this cycle's observations for nas_metrics.py to serve.

    This daemon is the ONLY producer of drive temperatures on this box, and
    that is a deliberate constraint rather than a convenience. The metrics
    endpoint must never poll drives itself: an HTTP consumer polling every
    10-30 s would turn every request into an ATA command, reset the idle
    timers, and silently defeat the cold-tier spindown -- the exact bug
    thermal_throttle.py originally had. This daemon already reads these
    values on its own schedule, and only for drives doing real block I/O, so
    routing everything through here costs nothing extra.

    Parked drives keep their last reading with an age, so a consumer can show
    them as stale rather than presenting an old number as if it were live.
    Written via a temp file + atomic replace so a reader never catches a
    half-written file.
    """
    now = time.time()
    for dev, t in dtemps.items():
        _drive_seen[dev] = {"temp": t, "ts": now}
    payload = {
        "ts": round(now, 1),
        "duty": duty,
        "duty_pct": round(duty * 100 / DUTY_MAX) if duty else None,
        "cpu_max": cpu,
        "temps": temps,
        "fans": {n: r for n, (_, r) in fans.items() if r is not None},
        "drives": {d: {"temp": v["temp"],
                       "age_s": round(now - v["ts"], 1),
                       "polled_this_cycle": d in dtemps}
                   for d, v in sorted(_drive_seen.items())},
    }
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, STATE)
    except OSError:
        pass                                  # telemetry must never break cooling


def failsafe(*_):
    """Never leave the box holding a low duty. Registered on every exit path."""
    for z in ZONE_REGS:
        subprocess.run(["ipmitool", "raw", "0x30", "0x91", "0x5a", "0x03",
                        ZONE_REGS[z], "0xff"], capture_output=True)
    note("EXIT -- fans forced to 0xff")
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, failsafe)
    signal.signal(signal.SIGINT, failsafe)

    note("=== fan control start: floor=%s max=%s interval=%ss%s ===" %
         (hex(DUTY_MIN), hex(DUTY_MAX), INTERVAL, " DRY-RUN" if DRY else ""))

    current = None
    cool_streak = 0
    try:
        while True:
            assert_full_mode()
            temps = sensor_temps()
            fans = fan_rpms()

            bad = [n for n, (st, _) in fans.items() if st != "ok"]
            if bad:
                if current != DUTY_MAX:
                    set_duty(0, DUTY_MAX); set_duty(1, DUTY_MAX)
                    current = DUTY_MAX
                    note("FAN FAULT %s -- pinned to 0xff" % ",".join(bad))
                if ONCE:
                    return
                time.sleep(INTERVAL)
                continue

            dtemps = {d: t for d in busy_drives() if (t := drive_temp(d)) is not None}
            dmax = max(dtemps.values()) if dtemps else None

            want, cpu = target_duty(temps, dmax)

            # Ramp up immediately; step down only after a sustained cool
            # streak, so a brief load spike does not start an oscillation.
            if current is None or want > current:
                cool_streak = 0
                change = True
            elif want < current:
                cool_streak += 1
                change = cool_streak >= DOWN_STREAK
            else:
                cool_streak = 0
                change = False

            if change and want != current:
                if set_duty(0, want) and set_duty(1, want):
                    note("DUTY %s -> %s  cpu=%sC periph=%sC drives=%s (%s)" %
                         (hex(current) if current else "-", hex(want), cpu,
                          temps.get("Peripheral Temp"),
                          ("%sC" % dmax) if dmax else "idle",
                          " ".join("%s=%s" % kv for kv in sorted(dtemps.items()))
                          or "none busy"))
                    current = want
                    cool_streak = 0
                else:
                    note("FAILED to set duty %s" % hex(want))

            write_state(current if current is not None else want,
                        cpu, temps, fans, dtemps)

            if ONCE:
                note("once: duty=%s cpu=%sC periph=%sC drives=%s fans=%s" %
                     (hex(want), cpu, temps.get("Peripheral Temp"),
                      dtemps or "none busy",
                      " ".join("%s:%s" % (n, r) for n, (_, r) in sorted(fans.items()))))
                return
            time.sleep(INTERVAL)
    except Exception as exc:                       # noqa: BLE001
        note("UNHANDLED %s: %s" % (type(exc).__name__, exc))
        failsafe()


if __name__ == "__main__":
    main()
