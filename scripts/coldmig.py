#!/usr/bin/env python3
"""Cold-tier migration: plan + execute + verify, self-contained.

Lives on Cloud36 (not /tmp) because this box has rebooted twice mid-job and
tmpfs loses everything.

Design decisions, all learned the hard way:

* rsync, never cp. cp chmods each directory it creates; on a dataset with
  aclmode=restricted that fails EPERM and cp ABORTS THE SUBTREE WHILE STILL
  EXITING 0 -- a silent no-op that looked like success.
* --partial so an interrupted title resumes instead of restarting.
* --bwlimit read fresh per title from BWFILE, so thermal_throttle.py can
  raise/lower the rate without restarting anything.
* Verify (file-count + per-file size + checksum sample) BEFORE deleting the
  source. Both crashes so far left sources fully intact because of this.
* Drives/pools identified by SERIAL, never letter: this box reshuffles
  letters across reboots (the Windows drive has been sdg, sdk, sdi; boot
  SSDs moved sdi/sdj -> sdk/sdl).
* Series are moved whole only when EVERY episode qualifies; per-season and
  per-episode variants live in per_season.py.

Selection criteria (owner-confirmed):
  - never watched (Plex view_count == 0)
  - acquired > 60 days ago  (anything newer stays hot -- "not watched YET")
  - <= 50 Mbps bitrate
  - not currently seeding in qBittorrent
  - fully-eligible series only; partially-watched handled separately

Usage: coldmig.py plan
       coldmig.py run <pool> [--bwlimit KBPS] [--dry-run]
"""
import datetime
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time

HOT = "/mnt/Cloud36/Fileshare/Services/Plex Data"
QBT_SHARE_ROOT = "/mnt/Cloud36/Fileshare"
SONARR_DB = "/mnt/.ix-apps/app_mounts/sonarr/config/sonarr.db"
RADARR_DB = "/mnt/.ix-apps/app_mounts/radarr/config/radarr.db"
BT_BACKUP = "/mnt/.ix-apps/app_mounts/qbittorrent/config/qBittorrent/BT_backup"
PLEX_DB = ("/mnt/Cloud36/Fileshare/Services/JARVIS/plex-config/Library/"
           "Application Support/Plex Media Server/Plug-in Support/Databases/"
           "com.plexapp.plugins.library.db")
STATE = "/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts"
PLAN = f"{STATE}/plan.json"
BWFILE = "/tmp/bwlimit_kbps"
BITRATE_CAP_MBPS = 50
RECENT_DAYS = 60
FILL_CAP = 0.85

PROTECTED = {
    HOT, f"{HOT}/Movies", f"{HOT}/Series", f"{HOT}/3DMovies", f"{HOT}/Preroll",
    "/mnt/Cloud36", "/mnt/Cloud36/Fileshare",
}
for _p in ("cold01", "cold02", "cold03", "cold04"):
    PROTECTED |= {f"/mnt/{_p}/media", f"/mnt/{_p}/media/Movies", f"/mnt/{_p}/media/Series"}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def assert_safe_to_remove(path):
    """Refuse to delete a library/tier root or anything shallower than
    <root>/<title>. A previous version derived the folder as dirname() of a
    media file, which for the 63 loose movies sitting directly in Movies/
    resolved to the ENTIRE Movies root."""
    rp = os.path.realpath(path)
    if rp in {os.path.realpath(p) for p in PROTECTED}:
        raise RuntimeError(f"REFUSING to remove protected path: {rp}")
    for root in (f"{HOT}/Movies", f"{HOT}/Series"):
        rr = os.path.realpath(root)
        if rp.startswith(rr + os.sep) and os.path.relpath(rp, rr) not in (".", ".."):
            return
    raise RuntimeError(f"REFUSING: {rp} not under a known library root")


# ---------------------------------------------------------------- bencode --
def _bdecode(data, i=0):
    c = data[i:i + 1]
    if c == b'i':
        j = data.index(b'e', i)
        return int(data[i + 1:j]), j + 1
    if c == b'l':
        i += 1
        out = []
        while data[i:i + 1] != b'e':
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b'd':
        i += 1
        out = {}
        while data[i:i + 1] != b'e':
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k.decode('utf-8', 'replace') if isinstance(k, bytes) else k] = v
        return out, i + 1
    if c.isdigit():
        j = data.index(b':', i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    raise ValueError("bad bencode")


def seeding_paths():
    out = set()
    if not os.path.isdir(BT_BACKUP):
        return out
    for fn in os.listdir(BT_BACKUP):
        if not fn.endswith(".fastresume"):
            continue
        try:
            with open(os.path.join(BT_BACKUP, fn), "rb") as fh:
                d, _ = _bdecode(fh.read())
        except Exception:
            continue
        sp = d.get("save_path", b"").decode("utf-8", "replace")
        mapped = d.get("mapped_files") or []
        name = d.get("qBt-name", d.get("name", b"")).decode("utf-8", "replace")
        top = (mapped[0].decode("utf-8", "replace").split("/")[0].removesuffix(".!qB")
               if mapped else name)
        if sp.startswith("/share") and top:
            p = os.path.join(sp.replace("/share", QBT_SHARE_ROOT, 1), top)
            if os.path.exists(p):
                out.add(os.path.realpath(p))
    return out


def plex_snapshot():
    """Plex's live WAL can exceed 200 MB during a scan; a read-only open then
    fails ('file is not a database') because RO cannot replay a WAL. Copy and
    read the copy -- the real DB is never touched."""
    snap = "/tmp/plexsnap.db"
    for s in ("", "-wal", "-shm"):
        if os.path.exists(PLEX_DB + s):
            shutil.copyfile(PLEX_DB + s, snap + s)
    con = sqlite3.connect(snap)
    con.create_collation("icu_root", lambda a, b: (a > b) - (a < b))
    con.create_collation("icu_case_sensitive", lambda a, b: (a > b) - (a < b))
    return con


# ------------------------------------------------------------------- plan --
def build_units():
    seeding = seeding_paths()
    log(f"seeding folders excluded: {len(seeding)}")
    cut = datetime.datetime.now() - datetime.timedelta(days=RECENT_DAYS)
    pcon = plex_snapshot()
    plex = {}
    for f, size, dur, vc in pcon.execute("""
            SELECT mp.file, mp.size, mi.duration, COALESCE(s.view_count,0)
            FROM metadata_items mi
            JOIN media_items med ON med.metadata_item_id = mi.id
            JOIN media_parts mp ON mp.media_item_id = med.id
            LEFT JOIN metadata_item_settings s ON s.guid = mi.guid
            WHERE mp.file LIKE '/share/%'"""):
        plex[f] = (size or 0, dur or 0, vc)
    pcon.close()

    def agg(prefix):
        n = tot = vc = 0
        br = 0.0
        for f, (size, dur, v) in plex.items():
            if f == prefix or f.startswith(prefix.rstrip("/") + "/"):
                n += 1
                tot += size
                vc += v
                if dur:
                    br = max(br, (size * 8) / (dur / 1000) / 1e6)
        return n, tot, vc, br

    def recent_files(db, table, col, rid):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(f"SELECT DateAdded FROM {table} WHERE {col}=?", (rid,)).fetchall()
            con.close()
        except sqlite3.Error:
            return 0
        n = 0
        for (d,) in rows:
            if not d:
                continue
            try:
                if datetime.datetime.fromisoformat(d.split(".")[0]) > cut:
                    n += 1
            except ValueError:
                pass
        return n

    units = []
    scon = sqlite3.connect(f"file:{SONARR_DB}?mode=ro", uri=True)
    series = scon.execute("SELECT Id, Path, Title FROM Series").fetchall()
    scon.close()
    for sid, path, title in series:
        if not path.startswith("/share/Series/"):
            continue
        host = path.replace("/share", HOT, 1)
        if not os.path.isdir(host) or os.path.realpath(host) in seeding:
            continue
        n, size, vc, br = agg(path)
        if n == 0 or size == 0 or vc > 0 or br == 0 or br > BITRATE_CAP_MBPS:
            continue
        if recent_files(SONARR_DB, "EpisodeFiles", "SeriesId", sid) > 0:
            continue          # acquired inside the 2-month window -> stays hot
        if any(os.path.realpath(os.path.join(dp, f)) in seeding
               for dp, _, fs in os.walk(host) for f in fs):
            continue
        units.append({"kind": "series", "db_id": sid, "title": title,
                      "container_path": path, "host": host, "size": size,
                      "bitrate": round(br, 1), "n_files": n})

    rcon = sqlite3.connect(f"file:{RADARR_DB}?mode=ro", uri=True)
    movies = rcon.execute("""SELECT m.Id, m.Path, md.Title FROM Movies m
                             JOIN MovieMetadata md ON md.Id = m.MovieMetadataId""").fetchall()
    rcon.close()
    for mid, path, title in movies:
        if not path.startswith("/share/Movies/"):
            continue
        host = path.replace("/share", HOT, 1)
        if os.path.realpath(host) in {os.path.realpath(p) for p in PROTECTED}:
            continue
        if not os.path.isdir(host) or os.path.realpath(host) in seeding:
            continue
        n, size, vc, br = agg(path)
        if n == 0 or size == 0 or vc > 0 or br == 0 or br > BITRATE_CAP_MBPS:
            continue
        if recent_files(RADARR_DB, "MovieFiles", "MovieId", mid) > 0:
            continue
        if any(os.path.realpath(os.path.join(dp, f)) in seeding
               for dp, _, fs in os.walk(host) for f in fs):
            continue
        units.append({"kind": "movie", "db_id": mid, "title": title,
                      "container_path": path, "host": host, "size": size,
                      "bitrate": round(br, 1), "n_files": n})

    units.sort(key=lambda u: -u["size"])
    return units


def cmd_plan():
    already = set()
    for pool in ("cold01", "cold02", "cold03", "cold04"):
        for sub in ("Movies", "Series"):
            d = f"/mnt/{pool}/media/{sub}"
            if os.path.isdir(d):
                already |= set(os.listdir(d))
    units = [u for u in build_units()
             if os.path.basename(u["host"].rstrip("/")) not in already]
    log(f"eligible: {len(units)} units, {sum(u['size'] for u in units)/1e12:.2f} TB")

    plan = []
    for pool in ("cold01", "cold02", "cold03", "cold04"):
        mnt = f"/mnt/{pool}/media"
        if not os.path.isdir(mnt):
            continue
        st = os.statvfs(mnt)
        cap = st.f_blocks * st.f_frsize
        budget = int(cap * FILL_CAP) - (cap - st.f_bavail * st.f_frsize)
        if budget <= 0:
            log(f"{pool}: at cap, skipping")
            continue
        run = 0
        for u in units:
            if u.get("_dest") or run + u["size"] > budget:
                continue
            u["_dest"] = pool
            u["_mnt"] = mnt
            plan.append(u)
            run += u["size"]
        log(f"{pool}: {run/1e9:.1f} GB assigned (budget {budget/1e9:.0f} GB)")

    os.makedirs(STATE, exist_ok=True)
    for _m in os.listdir(STATE):
        if _m.endswith(".done"):
            os.remove(os.path.join(STATE, _m))
    with open(PLAN, "w") as fh:
        json.dump(plan, fh, indent=1)
    log(f"PLAN: {len(plan)} units, {sum(u['size'] for u in plan)/1e12:.2f} TB -> {PLAN}")
    for u in plan[:15]:
        log(f"  [{u['_dest']}] {u['kind']:<6} {u['title'][:42]:<42} {u['size']/1e9:>7.1f} GB")


# -------------------------------------------------------------------- run --
def manifest(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root)] = os.path.getsize(p)
    return out


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(8 << 20), b""):
            h.update(c)
    return h.hexdigest()


def bwlimit(default):
    try:
        with open(BWFILE) as fh:
            v = int(fh.read().strip())
            if 1000 <= v <= 500000:
                return v
    except (OSError, ValueError):
        pass
    return default


def update_path(db, table, db_id, old, new):
    for attempt in range(8):
        try:
            con = sqlite3.connect(db, timeout=60)
            con.execute("PRAGMA busy_timeout=60000;")
            rc = con.execute(f"UPDATE {table} SET Path=? WHERE Id=? AND Path=?",
                             (new, db_id, old)).rowcount
            con.commit()
            con.close()
            return rc
        except sqlite3.OperationalError:
            time.sleep(2 + attempt * 2)
    return -1


def cmd_run(pool, default_bw, dry):
    with open(PLAN) as fh:
        plan = json.load(fh)
    mine = [u for u in plan if u.get("_dest") == pool]
    log(f"[{pool}] {len(mine)} units, {sum(u['size'] for u in mine)/1e9:.1f} GB")
    ok = skip = 0
    for u in mine:
        sub = "Series" if u["kind"] == "series" else "Movies"
        name = os.path.basename(u["host"].rstrip("/"))
        dst = os.path.join(u["_mnt"], sub, name)
        log(f"=== {u['kind']}: {u['title']} ({u['size']/1e9:.1f} GB) ===")
        if not os.path.isdir(u["host"]):
            log("  source gone (already migrated)")
            skip += 1
            continue
        try:
            assert_safe_to_remove(u["host"])
        except RuntimeError as e:
            log(f"  SAFETY GUARD: {e}")
            skip += 1
            continue
        if os.path.exists(dst):
            log("  partial dest -- resuming")
        if dry:
            log("  (dry run)")
            continue

        bw = bwlimit(default_bw)
        log(f"  rate cap {bw/1000:.0f} MB/s")
        os.makedirs(dst, exist_ok=True)
        t0 = time.time()
        r = subprocess.run(
            ["ionice", "-c", "2", "-n", "7", "rsync", "-rt", "--no-perms",
             "--no-owner", "--no-group", "--partial", f"--bwlimit={bw}",
             u["host"].rstrip("/") + "/", dst.rstrip("/") + "/"],
            capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  RSYNC rc={r.returncode}: {(r.stderr or '').strip()[-300:]}")
            skip += 1
            continue

        sm, dm = manifest(u["host"]), manifest(dst)
        if sm != dm:
            log(f"  MANIFEST MISMATCH ({len(sm)} vs {len(dm)}) -- both copies kept")
            skip += 1
            continue
        rels = list(sm)
        bad = [rel for rel in (rels if len(rels) <= 8 else random.sample(rels, 8))
               if md5(os.path.join(u["host"], rel)) != md5(os.path.join(dst, rel))]
        if bad:
            log(f"  CHECKSUM MISMATCH {bad} -- both copies kept")
            skip += 1
            continue
        el = time.time() - t0
        log(f"  verified {len(sm)} files in {el/60:.1f} min "
            f"({sum(sm.values())/1e6/max(el,1):.0f} MB/s)")

        assert_safe_to_remove(u["host"])
        shutil.rmtree(u["host"])
        newp = f"/{pool}/{sub}/{name}"
        db = SONARR_DB if u["kind"] == "series" else RADARR_DB
        tbl = "Series" if u["kind"] == "series" else "Movies"
        rc = update_path(db, tbl, u["db_id"], u["container_path"], newp)
        log(f"  {tbl}.Path -> {newp}" if rc == 1
            else f"  WARNING: {tbl}.Path not updated (rc={rc}) -- set manually to {newp}")
        ok += 1
    log(f"[{pool}] COMPLETE: {ok} migrated, {skip} skipped")
    # Marker so cold-migrate-launch.sh knows this pool is finished and
    # stops relaunching. cmd_plan() clears markers when a new plan is made.
    try:
        with open(f"{STATE}/{pool}.done", "w") as fh:
            fh.write(f"{time.strftime('%F %T')} {ok} migrated, {skip} skipped\n")
    except OSError:
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "plan":
        cmd_plan()
    elif sys.argv[1] == "run":
        bw = int(sys.argv[sys.argv.index("--bwlimit") + 1]) if "--bwlimit" in sys.argv else 20000
        cmd_run(sys.argv[2], bw, "--dry-run" in sys.argv)
    else:
        sys.exit(__doc__)
