#!/usr/bin/env python3
"""Move still-airing series from the cold tier back to hot storage.

WHY THIS EXISTS
---------------
coldmig.py moves whole series to the cold tier and repoints Sonarr at the
new location. That is correct for finished shows, but a series that is still
airing keeps receiving episodes -- and because Sonarr now believes the show
lives on /cold03, every new import writes to a cold drive. Observed on
2026-09-01: Bob's Burgers S16, Gilmore Girls S2, Regular Show and Gossip Girl
all had fresh episodes land on cold03 hours after the migration finished.

Two things go wrong when that happens:

  * The cold tier is not cold. cold_idle_spindown.py keys on real block I/O,
    so an import every few hours resets the idle clock permanently and the
    drive never parks. The whole point of the tier is lost.
  * cold03 fills. It is at 86% with 765 GB free, and a still-airing show has
    no natural end to its growth.

WHY WHOLE SERIES AND NOT INDIVIDUAL SEASONS
-------------------------------------------
Sonarr stores exactly ONE Path per series -- there is no per-season path. Its
root folders here are /share/Series (hot) and /cold01../cold03/Series, and a
series belongs to exactly one of them. Moving a single season back would put
files somewhere Sonarr does not know about: it would see them as missing,
re-download them, and import the replacements to the cold path anyway.

So "move still-airing seasons back" necessarily means "move the whole series
back". That is what this does. The alternative -- pointing Sonarr at a
mergerfs union so one series can span tiers -- is a much larger change and is
not what is deployed today.

SELECTION
---------
A series is "still airing" if Sonarr Status == 0 (continuing) or it has any
episode with a future air date. --with-future-only narrows that to shows with
episodes actually scheduled, which is the subset that will definitely write
to cold storage soon.

SAFETY
------
Same properties as coldmig.py, which moved 189 titles with zero errors:

  * rsync, never cp. cp chmods each directory it creates; on a dataset with
    aclmode=restricted that fails EPERM and cp ABORTS THE SUBTREE WHILE
    STILL EXITING 0. A 60 GB copy once "succeeded" in 0 seconds having moved
    0 of 156,687 files.
  * Verify before delete: full file+size manifest must match, plus md5 on a
    sample, or BOTH copies are kept and the title is skipped.
  * The delete guard is stricter than coldmig's: the target must be exactly
    one level below a cold-tier Series root. It can never resolve to a tier
    root, a library root, or a season directory.
  * Actively-seeding titles are skipped -- moving their files breaks the
    torrent. Same rule the cold migration used.

Usage: unmigrate.py plan [--with-future-only]
       unmigrate.py run  [--with-future-only] [--bwlimit KBPS] [--dry-run]
"""
import datetime
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coldmig                                                   # noqa: E402

HOT = coldmig.HOT
SONARR_DB = coldmig.SONARR_DB
POOLS = ("cold01", "cold02", "cold03", "cold04")
LOG = "/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/unmigrate.log"


def log(m):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%F"), line))
    except OSError:
        pass


def host_path(container_path):
    """/cold03/Series/X -> /mnt/cold03/media/Series/X"""
    for pool in POOLS:
        pref = "/%s/" % pool
        if container_path.startswith(pref):
            return "/mnt/%s/media/%s" % (pool, container_path[len(pref):])
    return None


def assert_safe_to_remove_cold(path):
    """Refuse anything that is not exactly <cold pool>/media/Series/<title>.

    Deliberately stricter than coldmig.assert_safe_to_remove: that one accepts
    any depth under a library root, which is fine when the caller always
    passes a title directory. Here the inputs come from a database, so the
    guard checks the shape itself -- a malformed or truncated path can never
    resolve to a tier root, a library root, or a single season directory.
    """
    rp = os.path.realpath(path)
    if rp in {os.path.realpath(p) for p in coldmig.PROTECTED}:
        raise RuntimeError("REFUSING to remove protected path: %s" % rp)
    for pool in POOLS:
        root = os.path.realpath("/mnt/%s/media/Series" % pool)
        if rp.startswith(root + os.sep):
            rel = os.path.relpath(rp, root)
            if rel not in (".", "..") and os.sep not in rel:
                return
    raise RuntimeError("REFUSING: %s is not <cold pool>/media/Series/<title>" % rp)


def candidates(future_only):
    """Still-airing series currently on the cold tier."""
    now = datetime.datetime.utcnow().isoformat()
    con = sqlite3.connect(SONARR_DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000;")
    rows = con.execute("""
        SELECT s.Id, s.Title, s.Path, s.Status,
               (SELECT COUNT(*) FROM Episodes e
                 WHERE e.SeriesId = s.Id AND e.AirDateUtc > ?) fut
        FROM Series s WHERE s.Path LIKE '/cold%'
    """, (now,)).fetchall()
    con.close()
    out = []
    for r in rows:
        fut = r["fut"] or 0
        if future_only and fut == 0:
            continue
        if not future_only and r["Status"] != 0 and fut == 0:
            continue
        src = host_path(r["Path"])
        if not src or not os.path.isdir(src):
            log("SKIP %s: source missing (%s)" % (r["Title"], src))
            continue
        out.append({"id": r["Id"], "title": r["Title"], "container": r["Path"],
                    "src": src, "name": os.path.basename(r["Path"].rstrip("/")),
                    "fut": fut, "status": r["Status"]})
    return sorted(out, key=lambda u: -u["fut"])


def size_of(root):
    n = 0
    for dp, _, fs in os.walk(root):
        for f in fs:
            try:
                n += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return n


def cmd_plan(future_only):
    units = candidates(future_only)
    total = 0
    log("%-36s %9s %5s  %s" % ("SERIES", "SIZE", "FUT", "FROM"))
    for u in units:
        u["bytes"] = size_of(u["src"])
        total += u["bytes"]
        log("%-36s %8.1fG %5s  %s" % (u["title"][:36], u["bytes"] / 2**30,
                                      u["fut"], u["container"]))
    log("%d series, %.1f GB to move back to hot" % (len(units), total / 2**30))
    free = shutil.disk_usage(HOT).free
    log("hot free: %.1f GB %s" % (free / 2**30,
                                  "OK" if free > total * 1.1 else "*** TIGHT ***"))
    return units


def cmd_run(future_only, bw, dry):
    units = cmd_plan(future_only)
    if dry:
        log("dry run -- nothing moved")
        return
    seeding = coldmig.seeding_paths()
    ok = skip = 0
    for u in units:
        log("=== %s (%.1f GB) ===" % (u["title"], u.get("bytes", 0) / 2**30))
        dst = os.path.join(HOT, "Series", u["name"])

        if any(str(p).startswith(u["src"]) for p in seeding):
            log("  SKIP: actively seeding -- moving would break the torrent")
            skip += 1
            continue
        if os.path.isdir(dst) and os.listdir(dst):
            log("  SKIP: destination already exists and is not empty: %s" % dst)
            skip += 1
            continue

        os.makedirs(dst, exist_ok=True)
        t0 = time.time()
        cmd = ["ionice", "-c", "2", "-n", "7", "rsync", "-rt", "--no-perms",
               "--no-owner", "--no-group", "--partial"]
        if bw:
            cmd.append("--bwlimit=%d" % bw)
        cmd += [u["src"].rstrip("/") + "/", dst.rstrip("/") + "/"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log("  RSYNC rc=%s: %s" % (r.returncode, (r.stderr or "").strip()[-300:]))
            skip += 1
            continue

        sm, dm = coldmig.manifest(u["src"]), coldmig.manifest(dst)
        if sm != dm:
            log("  MANIFEST MISMATCH (%d vs %d) -- both copies kept" % (len(sm), len(dm)))
            skip += 1
            continue
        rels = list(sm)
        bad = [rel for rel in (rels if len(rels) <= 8 else random.sample(rels, 8))
               if coldmig.md5(os.path.join(u["src"], rel))
               != coldmig.md5(os.path.join(dst, rel))]
        if bad:
            log("  CHECKSUM MISMATCH %s -- both copies kept" % bad)
            skip += 1
            continue
        el = time.time() - t0
        log("  verified %d files in %.1f min (%.0f MB/s)"
            % (len(sm), el / 60, sum(sm.values()) / 1e6 / max(el, 1)))

        assert_safe_to_remove_cold(u["src"])
        shutil.rmtree(u["src"])
        newp = "/share/Series/%s" % u["name"]
        rc = coldmig.update_path(SONARR_DB, "Series", u["id"], u["container"], newp)
        log("  Series.Path -> %s" % newp if rc == 1
            else "  WARNING: Series.Path not updated (rc=%s) -- set manually to %s"
                 % (rc, newp))
        ok += 1
    log("COMPLETE: %d moved back to hot, %d skipped" % (ok, skip))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    fo = "--with-future-only" in sys.argv
    bwl = int(sys.argv[sys.argv.index("--bwlimit") + 1]) if "--bwlimit" in sys.argv else 0
    if mode == "plan":
        cmd_plan(fo)
    elif mode == "run":
        cmd_run(fo, bwl, "--dry-run" in sys.argv)
    else:
        print(__doc__)
        sys.exit(2)
