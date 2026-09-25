#!/usr/bin/env python3
"""Push every new BMC power reading to Home Assistant as it happens.

nas_metrics.py already serves power_watts, but it is polled: it refreshes
every 10 s and HA only sees whatever it holds when HA asks. This daemon is
the push counterpart. It watches the BMC's instantaneous power reading and
POSTs each new value to HA's REST API the moment it appears, so the HA graph
shows every step the PSUs report, not a 10 s sample of them.

WHY IT READS THE BMC ITSELF
---------------------------
Going through :9101 would cap it at nas_metrics' 10 s refresh, and that
refresh is slow for a reason -- it bundles a full `ipmitool sdr` pass
(~1.5 s over KCS). Here only `ipmitool dcmi power reading` is issued, which
costs ~0.2 s. Kept as a separate process from nas_metrics.py so a bug here
cannot take down the endpoint HA and the dashboards already rely on.

WHAT "A NEW READING" MEANS ON THIS BMC
--------------------------------------
Measured 2026-09-25 over 3 min of 0.5 s polling: the value changes at
irregular intervals, anywhere from 0.5 s to 45 s apart (median a few
seconds), in steps of 3-10 W. The DCMI "IPMI timestamp" is useless as a
sample marker -- it is just the BMC clock and ticks every second whether
or not the reading moved. So a new reading is detected by the value
changing, and POLL_S is the floor of how fast a change can be seen.

BMC CONTENTION
--------------
Every ipmitool call shares the one KCS interface with fan_control.py and
nas_metrics.py, and fan control is what keeps the drives cool. A/B against
`ipmitool sdr type temperature` (the read fan_control does), median of 8:
  no pusher 1.7 s   |   POLL_S=1.0  1.8-2.3 s   |   POLL_S=0.5  2.5-3.0 s
No errors or timeouts at either rate, but 0.5 s cost fan control ~50% on
every sensor read to catch the rare change that lasts under a second. 1.0 s
is near-free and still sees nearly every change: most are seconds apart.

A reading that has not changed is re-sent every HEARTBEAT_S. HA does not
write a new state row for an identical state, only bumps last_reported,
so the heartbeat is nearly free on HA's side and it is what lets the
"NAS power" template sensor tell a steady load from a dead pusher.

The BMC and HA sides run in separate threads so a slow or unreachable HA
never delays a BMC poll. Only the newest reading is ever queued: after an
outage HA gets the current value, not a backlog.

The token file must NOT live anywhere shared: /mnt/Cloud36/Fileshare is an
SMB share and Services/JARVIS is NFS-exported to the VM. A long-lived HA
token is full admin on HA.

Usage: nas_power_push.py [--poll 1.0] [--once] [--dry-run]
  --poll S    seconds between BMC reads, start-to-start
  --once      one BMC read and one push, then exit (deploy check)
  --dry-run   poll and log changes, never contact HA
"""
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import homelab_env

LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/nas-power-push.log"
ENTITY = "sensor.nas_power_raw"
ATTRS = {                  # constant on purpose: a changing attribute makes
    "unit_of_measurement": "W",   # HA write a new attributes row per push
    "device_class": "power",
    "friendly_name": "NAS power (raw)",
    # No state_class: long-term statistics belong to the "NAS power"
    # template sensor built on top of this one, not to both.
}
HEARTBEAT_S = 30.0         # re-send an unchanged reading this often
BMC_FRESH_S = 10.0         # never heartbeat a value the BMC stopped confirming
BACKOFF_MAX_S = 60.0
SUMMARY_S = 3600           # one stats line an hour, not one per sample


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


POLL_S = arg("--poll", 1.0)    # start-to-start; see "BMC CONTENTION" above
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


_last_said = {}


def note_limited(kind, msg, every=300):
    """Log a recurring error at most once per `every` seconds per kind.

    An HA outage would otherwise write a line per failed push -- several a
    second -- onto the pool the log lives on.
    """
    now = time.time()
    if now - _last_said.get(kind, 0) >= every:
        _last_said[kind] = now
        note(msg)


def read_power():
    """(watts, None) from the BMC, or (None, why). Never raises.

    The reason matters: ENOSYS on exec means this process inherited cron's
    sudo seccomp filter (see nas-power-push-launch.sh) and will never read
    the BMC again, which is a different fix from a busy BMC.
    """
    try:
        r = subprocess.run(["ipmitool", "dcmi", "power", "reading"],
                           capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError) as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    m = re.search(r"Instantaneous power reading:\s+(\d+)", r.stdout)
    if r.returncode == 0 and m:
        return int(m.group(1)), None
    return None, "ipmitool rc=%d %s" % (r.returncode, r.stderr.strip()[:100])


class Latest:
    """The newest reading, handed from the poller to the pusher.

    A single slot rather than a queue: when HA is slow the pusher skips
    straight to the current value instead of replaying stale ones.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.watts = None
        self.seq = 0            # bumped on every change
        self.read_at = 0.0      # last successful BMC read, changed or not

    def offer(self, watts):
        with self.cond:
            self.read_at = time.time()
            if watts != self.watts:
                self.watts = watts
                self.seq += 1
                self.cond.notify()
                return True
        return False


class HA:
    def __init__(self, url, token_file):
        u = urlsplit(url)
        self.host, self.port = u.hostname, u.port or 8123
        self.token_file = token_file
        self.token = self._read_token()
        self.conn = None

    def _read_token(self):
        # Re-read on 401 so rotating the token needs no restart.
        with open(self.token_file) as fh:
            return fh.read().strip()

    def push(self, watts):
        """POST one state. Returns True on success. Never raises."""
        body = json.dumps({"state": str(watts), "attributes": ATTRS})
        headers = {"Authorization": "Bearer " + self.token,
                   "Content-Type": "application/json"}
        # Two attempts only when the first went over a reused connection:
        # HA closes idle keep-alive sockets, and that first failure says
        # nothing about whether HA is actually up.
        for attempt in (1, 2):
            reused = self.conn is not None
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port,
                                                           timeout=5)
                self.conn.request("POST", "/api/states/" + ENTITY, body, headers)
                resp = self.conn.getresponse()
                resp.read()                    # drain so keep-alive works
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
            note_limited("ha-401", "HA rejected the token (401) -- re-reading %s"
                         % self.token_file)
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


stats = {"reads": 0, "read_fail": 0, "changes": 0, "pushes": 0, "push_fail": 0}


def poller(latest, stop):
    """Read the BMC every POLL_S, start-to-start, calls never overlapping."""
    was_failing = False
    while not stop.is_set():
        started = time.monotonic()
        watts, why = read_power()
        if watts is None:
            stats["read_fail"] += 1
            was_failing = True
            note_limited("bmc", "BMC power read failed: %s" % why)
        else:
            stats["reads"] += 1
            if was_failing:
                note("BMC power reads recovered: %d W" % watts)
                was_failing = False
            if latest.offer(watts):
                stats["changes"] += 1
                if DRY:
                    note("dry-run: %d W" % watts)
        stop.wait(max(0.0, POLL_S - (time.monotonic() - started)))


def pusher(latest, ha, stop):
    sent_seq, sent_at, backoff = 0, 0.0, 0.0
    while not stop.is_set():
        with latest.cond:
            # Wake on a new reading, or when the heartbeat is due. Before
            # the first reading arrives there is nothing to heartbeat, so
            # just wait for one rather than spinning.
            wait = HEARTBEAT_S - (time.time() - sent_at)
            if latest.watts is None:
                latest.cond.wait(HEARTBEAT_S)
            elif latest.seq == sent_seq and wait > 0:
                latest.cond.wait(wait)
            seq, watts, read_at = latest.seq, latest.watts, latest.read_at
        if stop.is_set() or watts is None:
            continue
        is_new = seq != sent_seq
        if not is_new:
            if time.time() - sent_at < HEARTBEAT_S:
                continue
            if time.time() - read_at > BMC_FRESH_S:
                # BMC has gone quiet: stay silent so HA marks us stale
                # rather than heartbeating a number nobody is measuring.
                sent_at = time.time()
                continue
        if ha.push(watts):
            stats["pushes"] += 1
            sent_seq, sent_at, backoff = seq, time.time(), 0.0
        else:
            stats["push_fail"] += 1
            backoff = min(BACKOFF_MAX_S, max(1.0, backoff * 2))
            stop.wait(backoff)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if ONCE:
        watts, why = read_power()
        print("BMC: %s W%s" % (watts, "" if why is None else " (%s)" % why))
        if watts is not None and not DRY:
            ha = HA(homelab_env.get("HA_URL"), homelab_env.get("HA_TOKEN_FILE"))
            ok = ha.push(watts)
            print("push to HA %s: %s" % (ENTITY, "ok" if ok else "FAILED"))
            sys.exit(0 if ok else 1)
        sys.exit(0 if watts is not None else 1)

    ha = None
    if not DRY:
        ha = HA(homelab_env.get("HA_URL"), homelab_env.get("HA_TOKEN_FILE"))
    note("=== nas power push start: poll=%ss heartbeat=%ss entity=%s%s ===" %
         (POLL_S, HEARTBEAT_S, ENTITY, " (dry-run)" if DRY else ""))

    latest, stop = Latest(), threading.Event()
    threads = [threading.Thread(target=poller, args=(latest, stop), daemon=True)]
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
                note("last hour: reads=%(reads)d read_fail=%(read_fail)d "
                     "changes=%(changes)d pushes=%(pushes)d "
                     "push_fail=%(push_fail)d" % stats)
                for k in stats:
                    stats[k] = 0
        note("a worker thread died -- exiting so the watchdog restarts us")
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
