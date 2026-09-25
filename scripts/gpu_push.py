#!/usr/bin/env python3
"""Push the jarvis VM's GPU power and temperature to Home Assistant.

The Tesla P4 is passed through to this VM (vfio-pci on the TrueNAS host), so
the host cannot see it at all -- this has to run inside the VM. Same design
as nas_power_push.py on the NAS: every new reading is POSTed to HA's REST API
as it appears, into two raw entities, and the "Tesla P4 power" / "Tesla P4
temperature" template helpers in HA sit on top of those.

READING THE GPU
---------------
One long-running `nvidia-smi --query-gpu=... -lms 1000` streams a CSV line
per second, rather than forking nvidia-smi per sample. nvidia-persistenced
keeps the driver initialised, so each query is cheap. If nvidia-smi exits,
prints something that is not a reading, or goes silent (a wedged driver),
it is killed and restarted with backoff, and the heartbeat stops so HA
marks the sensors unavailable instead of holding a stale number.

WHAT COUNTS AS A NEW READING
----------------------------
NVML reports power to 0.01 W and every sample differs (idle measured at
6.96-7.83 W, 2026-09-25), so pushing on raw change would write a state row
per second forever. Power is rounded to whole watts, the same resolution
the BMC gives nas_power_push.py; temperature is already whole degrees. Each
entity is pushed when its rounded value changes and re-sent every
HEARTBEAT_S otherwise (HA only bumps last_reported for an identical state).

Config comes from the environment (systemd EnvironmentFile=/etc/homelab.env,
the VM-side convention -- see systemd/gpu-push.service):
  HA_URL          e.g. http://192.0.2.30:8123
  HA_TOKEN_FILE   optional; defaults to the unit's LoadCredential copy,
                  $CREDENTIALS_DIRECTORY/ha-token

Usage: gpu_push.py [--once] [--dry-run]
"""
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

GPU = "tesla_p4"                      # entity id prefix
LABEL = "Tesla P4"
# entity -> (nvidia-smi field, unit, device_class, value parser)
FIELDS = [
    ("sensor.%s_power_raw" % GPU, "power.draw", "W", "power",
     lambda s: int(round(float(s)))),
    ("sensor.%s_temperature_raw" % GPU, "temperature.gpu", "°C", "temperature",
     lambda s: int(s)),
]
SAMPLE_MS = 1000
HEARTBEAT_S = 30.0
SILENT_S = 10.0            # no line from nvidia-smi for this long = wedged
BACKOFF_MAX_S = 60.0
SUMMARY_S = 3600

ONCE = "--once" in sys.argv
DRY = "--dry-run" in sys.argv


def note(msg):
    # stdout only: journald timestamps and keeps it (journalctl -u gpu-push).
    print(msg, flush=True)


_last_said = {}


def note_limited(kind, msg, every=300):
    """Log a recurring error at most once per `every` seconds per kind."""
    now = time.time()
    if now - _last_said.get(kind, 0) >= every:
        _last_said[kind] = now
        note(msg)


def attrs_for(entity, unit, device_class):
    # Constant per entity: a changing attribute makes HA write a new
    # attributes row on every push. No state_class -- long-term statistics
    # belong to the template helpers, not to these raw entities as well.
    kind = "power" if device_class == "power" else "temperature"
    return {"unit_of_measurement": unit, "device_class": device_class,
            "friendly_name": "%s %s (raw)" % (LABEL, kind)}


class Latest:
    """Newest value per entity, handed from the reader to the pusher."""

    def __init__(self):
        self.cond = threading.Condition()
        self.values = {}           # entity -> value
        self.seq = {}              # entity -> bumped on every change
        self.read_at = 0.0         # last good line from nvidia-smi

    def offer(self, readings):
        changed = False
        with self.cond:
            self.read_at = time.time()
            for entity, value in readings.items():
                if self.values.get(entity) != value:
                    self.values[entity] = value
                    self.seq[entity] = self.seq.get(entity, 0) + 1
                    changed = True
            if changed:
                self.cond.notify()
        return changed


class HA:
    def __init__(self, url, token_file):
        u = urlsplit(url)
        self.host, self.port = u.hostname, u.port or 8123
        self.token_file = token_file
        self.token = self._read_token()
        self.conn = None

    def _read_token(self):
        with open(self.token_file) as fh:
            return fh.read().strip()

    def push(self, entity, value, attrs):
        """POST one state. Returns True on success. Never raises."""
        body = json.dumps({"state": str(value), "attributes": attrs})
        headers = {"Authorization": "Bearer " + self.token,
                   "Content-Type": "application/json"}
        # A failure on a reused keep-alive socket is usually just HA having
        # closed it while idle, so that one gets a single fresh retry.
        for attempt in (1, 2):
            reused = self.conn is not None
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port,
                                                           timeout=5)
                self.conn.request("POST", "/api/states/" + entity, body, headers)
                resp = self.conn.getresponse()
                resp.read()
                break
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                if reused and attempt == 1:
                    continue
                note_limited("ha-io", "HA unreachable: %s: %s" %
                             (type(exc).__name__, str(exc)[:100]))
                return False
        if resp.status in (200, 201):
            return True
        if resp.status == 401:
            note_limited("ha-401", "HA rejected the token (401) -- re-reading it")
            try:
                self.token = self._read_token()
            except OSError as exc:
                note_limited("token", "cannot read token file: %s" % exc)
        else:
            note_limited("ha-%d" % resp.status, "HA returned HTTP %d" % resp.status)
        return False

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None


stats = {"lines": 0, "bad": 0, "restarts": 0, "pushes": 0, "push_fail": 0}


def parse(line):
    """One CSV line -> {entity: value}, or None if it is not a reading."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != len(FIELDS):
        return None
    try:
        return {f[0]: f[4](p) for f, p in zip(FIELDS, parts)}
    except ValueError:                 # "[N/A]", "[Unknown Error]", ...
        return None


def query_cmd(loop):
    cmd = ["nvidia-smi", "--id=0",
           "--query-gpu=" + ",".join(f[1] for f in FIELDS),
           "--format=csv,noheader,nounits"]
    return cmd + ["-lms", str(SAMPLE_MS)] if loop else cmd


def reader(latest, stop):
    """Keep one nvidia-smi streaming; restart it if it dies or goes quiet."""
    backoff = 0.0
    while not stop.is_set():
        try:
            proc = subprocess.Popen(query_cmd(loop=True), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            note_limited("spawn", "cannot start nvidia-smi: %s" % exc)
            backoff = min(BACKOFF_MAX_S, max(5.0, backoff * 2))
            stop.wait(backoff)
            continue

        last_line = [time.time()]

        def watchdog():
            # readline() below blocks forever on a wedged nvidia-smi, so a
            # side thread enforces SILENT_S by killing it.
            while proc.poll() is None and not stop.is_set():
                if time.time() - last_line[0] > SILENT_S:
                    note_limited("silent", "nvidia-smi silent for %ds -- restarting"
                                 % SILENT_S)
                    proc.kill()
                    return
                time.sleep(1)
            if stop.is_set() and proc.poll() is None:
                proc.kill()

        threading.Thread(target=watchdog, daemon=True).start()
        good = False
        for line in proc.stdout:
            last_line[0] = time.time()
            readings = parse(line)
            if readings is None:
                stats["bad"] += 1
                note_limited("bad", "unexpected nvidia-smi output: %r" % line.strip()[:120])
                continue
            stats["lines"] += 1
            if not good:
                good, backoff = True, 0.0
            if latest.offer(readings) and DRY:
                note("dry-run: %s" % readings)
        proc.wait()
        if stop.is_set():
            return
        stats["restarts"] += 1
        note_limited("exit", "nvidia-smi exited rc=%s -- restarting" % proc.returncode)
        backoff = 1.0 if good else min(BACKOFF_MAX_S, max(5.0, backoff * 2))
        stop.wait(backoff)


def pusher(latest, ha, stop):
    meta = {f[0]: attrs_for(f[0], f[2], f[3]) for f in FIELDS}
    sent_seq = {}
    sent_at = {e: 0.0 for e in meta}
    backoff = 0.0
    while not stop.is_set():
        with latest.cond:
            now = time.time()
            due = min(HEARTBEAT_S - (now - t) for t in sent_at.values())
            pending = any(latest.seq.get(e, 0) != sent_seq.get(e, 0) for e in meta)
            if not pending:
                latest.cond.wait(max(0.5, due) if latest.values else HEARTBEAT_S)
            values, seqs, read_at = dict(latest.values), dict(latest.seq), latest.read_at
        if stop.is_set():
            return
        fresh = time.time() - read_at <= SILENT_S
        failed = False
        for entity, value in values.items():
            is_new = seqs.get(entity, 0) != sent_seq.get(entity, 0)
            beat = time.time() - sent_at[entity] >= HEARTBEAT_S
            if not is_new and not (beat and fresh):
                continue
            if ha.push(entity, value, meta[entity]):
                stats["pushes"] += 1
                sent_seq[entity], sent_at[entity] = seqs.get(entity, 0), time.time()
            else:
                stats["push_fail"] += 1
                failed = True
                break
        if not fresh:
            # GPU gone quiet: stop heartbeating so HA marks the sensors
            # unavailable, and don't spin while waiting for it to return.
            for e in sent_at:
                sent_at[e] = time.time()
        if failed:
            backoff = min(BACKOFF_MAX_S, max(1.0, backoff * 2))
            stop.wait(backoff)
        else:
            backoff = 0.0


def token_file():
    path = os.environ.get("HA_TOKEN_FILE")
    if not path and os.environ.get("CREDENTIALS_DIRECTORY"):
        path = os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "ha-token")
    if not path:
        sys.exit("no HA token: set HA_TOKEN_FILE or run under gpu-push.service")
    return path


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if ONCE:
        r = subprocess.run(query_cmd(loop=False), capture_output=True, text=True)
        readings = parse(r.stdout.strip())
        print("GPU: %s" % (readings or r.stdout.strip() or r.stderr.strip()))
        if readings and not DRY:
            ha = HA(os.environ["HA_URL"], token_file())
            meta = {f[0]: attrs_for(f[0], f[2], f[3]) for f in FIELDS}
            ok = all(ha.push(e, v, meta[e]) for e, v in readings.items())
            print("push to HA: %s" % ("ok" if ok else "FAILED"))
            sys.exit(0 if ok else 1)
        sys.exit(0 if readings else 1)

    ha = None
    if not DRY:
        if not os.environ.get("HA_URL"):
            sys.exit("HA_URL not set -- see systemd/gpu-push.service")
        ha = HA(os.environ["HA_URL"], token_file())
    note("=== gpu push start: sample=%dms heartbeat=%ss entities=%s%s ===" %
         (SAMPLE_MS, HEARTBEAT_S, ",".join(f[0] for f in FIELDS),
          " (dry-run)" if DRY else ""))

    latest, stop = Latest(), threading.Event()
    threads = [threading.Thread(target=reader, args=(latest, stop), daemon=True)]
    if ha is not None:
        threads.append(threading.Thread(target=pusher, args=(latest, ha, stop),
                                        daemon=True))
    for t in threads:
        t.start()

    try:
        next_summary = time.time() + SUMMARY_S
        while all(t.is_alive() for t in threads):
            time.sleep(5)
            if time.time() >= next_summary:
                next_summary += SUMMARY_S
                note("last hour: lines=%(lines)d bad=%(bad)d restarts=%(restarts)d "
                     "pushes=%(pushes)d push_fail=%(push_fail)d" % stats)
                for k in stats:
                    stats[k] = 0
        note("a worker thread died -- exiting so systemd restarts us")
        sys.exit(1)
    finally:
        stop.set()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:                                  # noqa: BLE001
        note("UNHANDLED %s: %s" % (type(exc).__name__, exc))
        sys.exit(1)
