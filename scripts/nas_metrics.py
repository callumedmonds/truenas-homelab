#!/usr/bin/env python3
"""Read-only metrics endpoint for Home Assistant.

Serves JSON at http://<nas>:9101/metrics for an HA `rest` sensor. HA polls
the NAS rather than the NAS pushing to HA, which means NO CREDENTIALS ARE
INVOLVED anywhere: no HA long-lived token, no BMC username/password. That is
the whole reason for this design over the HACS "IPMI Server" integration,
which needs BMC credentials.

THE ONE RULE: THIS PROCESS NEVER TOUCHES A DRIVE
------------------------------------------------
Everything it serves is either free to read, or already collected by a
daemon that was going to read it anyway:

  power / CPU / system temps / fan RPM   live from ipmitool (BMC only, no
                                         storage involvement whatsoever)
  drive temperatures                     from fan-state.json, written by
                                         fan_control.py every 30 s
  drive park state                       from spindown-state.json, written
                                         by cold_idle_spindown.py

If this endpoint polled drives directly, an HA sensor scanning every 10-30 s
would issue an ATA command per drive per request. Those commands reset the
drive idle timer WITHOUT registering in /proc/diskstats, so the cold tier
would never reach standby -- silently undoing cold_idle_spindown.py. That is
not hypothetical: it is exactly the bug thermal_throttle.py shipped with.
`smartctl -n standby` does not rescue you either, since it only skips drives
ALREADY asleep; an awake drive still gets polled and can never fall asleep.

So drive data here is deliberately second-hand and carries an age. A parked
drive reports its last known temperature with a large age_s and
state="standby" -- never a stale number dressed up as live.

BMC POLLING
-----------
Measured on this box: the BMC refreshes its instantaneous power reading
about every 1-2 s, with +/-3-4 W of jitter, and each KCS read costs ~240 ms.
A background thread collects every REFRESH_S and requests are answered from
that snapshot, so BMC load is fixed at one poll per REFRESH_S no matter how
many consumers exist, and no HTTP request ever waits on the BMC. Collecting
on the request path instead made every call take ~3.4 s.

Usage: nas_metrics.py [--port 9101] [--bind 0.0.0.0] [--once]
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAN_STATE = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/fan-state.json"
SPIN_STATE = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/spindown-state.json"
LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/nas-metrics.log"
REFRESH_S = 10.0       # background collection interval (see refresher())
STALE_S = 300          # fan-state older than this is reported as stale


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


PORT = arg("--port", 9101)
BIND = arg("--bind", "0.0.0.0")


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
    # OSError matters as much as SubprocessError here: it is NOT a subclass,
    # so an exec failure (ENOENT if ipmitool goes missing, ENOSYS if the spawn
    # path is unavailable) would otherwise escape and kill the refresh loop.
    # A metrics endpoint must degrade to nulls, never fall over.
    try:
        r = subprocess.run(["ipmitool"] + list(args), capture_output=True,
                           text=True, timeout=15)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def read_json(path, max_age=None):
    """Return (payload, age_seconds) or (None, None). Never raises."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        age = time.time() - float(data.get("ts", 0))
        if max_age is not None and age > max_age:
            data["_stale"] = True
        return data, round(age, 1)
    except (OSError, ValueError, TypeError):
        return None, None


def collect():
    """Build the payload. Only BMC reads happen here -- never storage."""
    out = {"ts": round(time.time(), 1)}

    m = re.search(r"Instantaneous power reading:\s+(\d+)",
                  ipmi("dcmi", "power", "reading"))
    out["power_watts"] = int(m.group(1)) if m else None

    # ONE `ipmitool sdr` pass for both temps and fans. Three separate calls
    # (dcmi + sdr type temperature + sdr type fan) cost ~3.4 s total over the
    # slow KCS interface; folding the two sdr calls into one roughly halves
    # that, and the background refresher below keeps it off the request path
    # entirely.
    want = {"CPU1 Temp": "cpu1_temp", "CPU2 Temp": "cpu2_temp",
            "System Temp": "system_temp", "Peripheral Temp": "peripheral_temp"}
    fans = {}
    for line in ipmi("sdr").splitlines():
        p = [x.strip() for x in line.split("|")]
        if len(p) < 2:
            continue
        name, val = p[0], p[1]
        if name in want:
            d = re.match(r"(\d+)\s*degrees", val)
            if d:
                out[want[name]] = int(d.group(1))
        elif name.startswith("FAN"):
            r = re.match(r"(\d+)\s*RPM", val)
            if r:
                fans[name.lower()] = int(r.group(1))
    out["fans"] = fans

    # --- second-hand, never polled here (see module docstring) -------------
    fan_state, fan_age = read_json(FAN_STATE, STALE_S)
    spin_state, spin_age = read_json(SPIN_STATE)

    out["fan_duty"] = (fan_state or {}).get("duty")
    out["fan_duty_pct"] = (fan_state or {}).get("duty_pct")
    out["fan_state_age_s"] = fan_age
    out["fan_control_running"] = bool(fan_state and not fan_state.get("_stale"))

    pool_of = {}
    for pool, info in ((spin_state or {}).get("drives") or {}).items():
        pool_of[info.get("dev")] = (pool, info.get("state"))

    pools = pool_map()
    drives = {}
    for dev, d in ((fan_state or {}).get("drives") or {}).items():
        pool, state = pool_of.get(dev, (None, None))
        pool = pool or pools.get(dev)
        if state is None:
            # Not managed by the spindown daemon: infer from whether
            # fan_control saw block I/O for it this cycle.
            state = "active" if d.get("polled_this_cycle") else "idle"
        drives[dev] = {
            "pool": pool,
            "state": state,
            "temp": None if state == "standby" else d.get("temp"),
            "last_temp": d.get("temp"),
            "age_s": d.get("age_s"),
        }
    # Drives the spindown daemon manages but fan_control has never polled
    # (parked since before this daemon started) still deserve an entry.
    for dev, (pool, state) in pool_of.items():
        drives.setdefault(dev, {"pool": pool or pools.get(dev), "state": state,
                                "temp": None, "last_temp": None, "age_s": None})

    # Seed every pool member, even ones fan_control has never polled. A drive
    # that has been quiet since this daemon started is legitimately absent
    # from fan-state.json, but a consumer wants a stable set of entities --
    # HA creates and destroys them as they come and go otherwise. "idle" here
    # means "no block I/O seen", which is distinct from "standby" (actually
    # parked, and only the spindown daemon can assert that).
    for dev, pool in pools.items():
        drives.setdefault(dev, {"pool": pool, "state": "idle", "temp": None,
                                "last_temp": None, "age_s": None})
    out["drives"] = drives
    out["drives_spun_down"] = sum(1 for d in drives.values()
                                  if d["state"] == "standby")
    active = [d["temp"] for d in drives.values() if d["temp"] is not None]
    out["drive_temp_max"] = max(active) if active else None

    # Per-pool max temperature: the shape HA actually wants for a sensor.
    per_pool = {}
    for d in drives.values():
        if d["pool"] and d["temp"] is not None:
            per_pool[d["pool"]] = max(per_pool.get(d["pool"], 0), d["temp"])
    out["pool_temp_max"] = per_pool
    out["spindown_state_age_s"] = spin_age
    return out


_pool_map = {"at": 0.0, "map": {}}


def pool_map():
    """device -> pool name, from ZFS's own view. Cached for 5 minutes.

    Free: `zpool status` reads ZFS in-memory state and `readlink` walks
    /dev symlinks. Neither issues an ATA command, so this does not
    interfere with spindown. Resolved fresh rather than hardcoded because
    device letters reshuffle across reboots on this box -- cold01 has been
    sdh, sdi and sdm; the old Windows drive has been sdg, sdh, sdi and sdk.
    """
    now = time.time()
    if _pool_map["map"] and now - _pool_map["at"] < 300:
        return _pool_map["map"]
    out = {}
    try:
        pools = subprocess.run(["zpool", "list", "-H", "-o", "name"],
                               capture_output=True, text=True, timeout=15).stdout.split()
        for pool in pools:
            txt = subprocess.run(["zpool", "status", "-P", pool],
                                 capture_output=True, text=True, timeout=20).stdout
            for tok in re.findall(r"(/dev/\S+)", txt):
                path = tok
                try:
                    if os.path.islink(path):
                        path = os.path.realpath(path)
                except OSError:
                    continue
                m = re.match(r"/dev/(sd[a-z]+|nvme\d+n\d+)", path)
                if m:
                    out[m.group(1)] = pool
    except (subprocess.SubprocessError, OSError):
        return _pool_map["map"]
    if out:
        _pool_map["map"] = out
        _pool_map["at"] = now
    return _pool_map["map"]


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def refresher():
    """Collect on a fixed timer, off the request path.

    Serving straight from collect() made every HTTP request wait ~3.4 s on
    the BMC's KCS interface, and it tied BMC load to how often consumers
    poll -- add a second dashboard and you double it. A background timer
    inverts that: requests are answered instantly from the last snapshot,
    and the BMC sees exactly one poll every REFRESH_S no matter how many
    consumers there are.

    REFRESH_S=10 sits comfortably under any sane HA scan_interval while
    staying well above the BMC's own ~1-2 s update rate, so nothing is lost.
    """
    while True:
        try:
            data = collect()
            with _lock:
                _cache["data"], _cache["at"] = data, time.time()
        except Exception as exc:                             # noqa: BLE001
            note("collect failed: %s: %s" % (type(exc).__name__, str(exc)[:100]))
        time.sleep(REFRESH_S)


def cached():
    with _lock:
        data, at = _cache["data"], _cache["at"]
    if data is None:                       # first request before first refresh
        data = collect()
        with _lock:
            _cache["data"], _cache["at"] = data, time.time()
        return data
    data = dict(data)
    data["age_s"] = round(time.time() - at, 1)
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                        # noqa: N802
        if self.path.split("?")[0] not in ("/", "/metrics"):
            self.send_error(404)
            return
        try:
            body = json.dumps(cached(), indent=1).encode()
        except Exception as exc:                             # noqa: BLE001
            self.send_error(500, str(exc)[:120])
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass                     # HA polls constantly; do not fill the disk


def main():
    if "--once" in sys.argv:
        print(json.dumps(collect(), indent=1))
        return
    note("=== nas_metrics listening on %s:%s (refresh=%ss) ===" % (BIND, PORT, REFRESH_S))
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
