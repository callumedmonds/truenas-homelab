#!/usr/bin/env python3
"""Turn the old Windows drive (HGST, serial SERIAL0004) into cold04.

Runs ONLY after the rescue keep-list is provably complete. The wipe is gated
on a full re-verification -- file count, every file's size, and a checksum
sample per item. If ANY item fails, nothing is wiped and the script exits
non-zero. Owner authorised the wipe on 2026-08-27 ("once data is copied off
cold4 wipe it and fill it"); this enforces the "once data is copied off".

Target is resolved by SERIAL, never by letter. This drive alone has appeared
as sdg, sdk, sdi and sdh across reboots, and the boot SSDs have occupied
those same letters -- a letter-based wipe would eventually destroy a boot
disk. The script also refuses to touch a disk that is part of any pool.

Then: pool cold04 -> dataset cold04/media (recordsize 1M, atime off, lz4) ->
NFSv4 ACLs matching the other tiers (owner@ + group:apps(568) +
group:Callum(1000), all inheritable) -> NFS export -> VM mount.

Usage: make_cold04.py [--verify-only] [--yes]
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time

SERIAL = "SERIAL0004"
POOL = "cold04"
DST = "/mnt/Cloud36/Fileshare/Recovered-from-sdg"
SRC = "/mnt/WindowsDrive"
ITEMS = [("DFL", "DFL"), ("ks backup", "ks-backup-full"), ("Users/OWNER", "Users-OWNER"),
         ("grace", "grace")]
VERIFY_ONLY = "--verify-only" in sys.argv
ASSUME_YES = "--yes" in sys.argv


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def midclt(*args, job=False):
    cmd = ["midclt", "call"] + (["-j"] if job else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:2])}: {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def tree(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            try:
                out[os.path.relpath(p, root)] = os.path.getsize(p)
            except OSError:
                pass
    return out


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(8 << 20), b""):
            h.update(c)
    return h.hexdigest()


def verify_all():
    """Every keep-list item must match exactly. Returns True only if all pass."""
    if not os.path.ismount(SRC):
        log(f"{SRC} is not mounted -- cannot verify, refusing to continue")
        return False
    ok = True
    for src_rel, dst_name in ITEMS:
        s, d = os.path.join(SRC, src_rel), os.path.join(DST, dst_name)
        if not os.path.isdir(s):
            log(f"  {src_rel}: source missing on the Windows drive -- skipping")
            continue
        if not os.path.isdir(d):
            log(f"  {src_rel}: NOT RESCUED (no {d})")
            ok = False
            continue
        sm, dm = tree(s), tree(d)
        if len(sm) != len(dm):
            log(f"  {src_rel}: FAIL count {len(sm)} vs {len(dm)}")
            ok = False
            continue
        bad = [k for k in sm if sm[k] != dm.get(k)]
        if bad:
            log(f"  {src_rel}: FAIL {len(bad)} size mismatches e.g. {bad[:3]}")
            ok = False
            continue
        rels = list(sm)
        sample = rels if len(rels) <= 10 else random.sample(rels, 10)
        mism = [k for k in sample if md5(os.path.join(s, k)) != md5(os.path.join(d, k))]
        if mism:
            log(f"  {src_rel}: FAIL checksum {mism}")
            ok = False
            continue
        log(f"  {src_rel}: OK  {len(sm)} files, {sum(sm.values())/1e9:.1f} GB")
    return ok


def resolve_target():
    disks = midclt("disk.query")
    match = [d for d in disks if d["serial"] == SERIAL]
    if len(match) != 1:
        raise RuntimeError(f"expected exactly 1 disk with serial {SERIAL}, got {len(match)}")
    d = match[0]
    # Refuse if it belongs to any pool.
    status = subprocess.run(["zpool", "status"], capture_output=True, text=True).stdout
    if d["name"] in status:
        raise RuntimeError(f"{d['name']} appears in zpool status -- REFUSING to wipe")
    return d


def main():
    log("=== verifying rescued keep-list before any destructive step ===")
    if not verify_all():
        log("VERIFICATION FAILED -- nothing wiped. Fix the rescue and re-run.")
        sys.exit(1)
    log("all keep-list items verified")
    if VERIFY_ONLY:
        log("--verify-only: stopping here")
        return

    d = resolve_target()
    log(f"target: {d['name']} serial={d['serial']} model={d.get('model')} "
        f"size={d['size']/1e9:.0f} GB")
    if not ASSUME_YES:
        log("refusing to wipe without --yes")
        sys.exit(1)

    log(f"unmounting {SRC}")
    subprocess.run(["umount", SRC], capture_output=True)

    log(f"wiping {d['name']} (QUICK: zeros first+last 32MB, clears signatures)")
    midclt("disk.wipe", d["name"], "QUICK", job=True)

    log(f"creating pool {POOL}")
    midclt("pool.create", json.dumps({
        "name": POOL,
        "topology": {"data": [{"type": "STRIPE", "disks": [d["name"]]}]},
    }), job=True)

    log(f"creating dataset {POOL}/media")
    midclt("pool.dataset.create", json.dumps({
        "name": f"{POOL}/media", "recordsize": "1M", "atime": "OFF",
        "compression": "LZ4", "acltype": "NFSV4", "aclmode": "RESTRICTED",
    }))

    mnt = f"/mnt/{POOL}/media"
    for sub in ("Movies", "Series"):
        os.makedirs(os.path.join(mnt, sub), exist_ok=True)

    log("applying NFSv4 ACLs (owner@ + group:apps + group:Callum, inheritable)")
    FULL = {"BASIC": "FULL_CONTROL"}
    INHERIT = {"BASIC": "INHERIT"}
    midclt("filesystem.setacl", json.dumps({
        "path": mnt,
        "dacl": [
            {"tag": "owner@", "id": -1, "type": "ALLOW", "perms": FULL, "flags": INHERIT},
            {"tag": "group@", "id": -1, "type": "ALLOW", "perms": FULL, "flags": INHERIT},
            {"tag": "GROUP", "id": 568, "type": "ALLOW", "perms": FULL, "flags": INHERIT},
            {"tag": "GROUP", "id": 1000, "type": "ALLOW", "perms": FULL, "flags": INHERIT},
        ],
        "uid": 1000, "gid": 1000, "acltype": "NFS4",
        "options": {"recursive": True, "traverse": False, "canonicalize": True},
    }), job=True)

    log("creating NFS export")
    try:
        midclt("sharing.nfs.create", json.dumps({
            "path": mnt, "networks": ["192.0.2.0/24"],
            "maproot_user": "root", "maproot_group": "root",
            "comment": "cold04 media",
        }))
    except RuntimeError as e:
        log(f"  NFS export note: {e}")

    log(f"{POOL} ready at {mnt}")
    log("NEXT: add the VM fstab entry + /cold04 mounts, then re-plan and run.")


if __name__ == "__main__":
    main()
