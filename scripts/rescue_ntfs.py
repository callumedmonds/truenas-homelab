#!/usr/bin/env python3
"""Rescue a keep-list off an NTFS volume to Cloud36, using ntfs-3g (userspace).

WHY NOT THE KERNEL DRIVER: TrueNAS's in-kernel `ntfs3` is the prime suspect
for two hard crashes on 2026-08-27. Both came minutes after an NTFS read
began (5 min, then 15 min) -- the second at a throttled 20 MB/s with drives
at 43-48 C, so neither heat nor bandwidth explains it. The BMC logged
nothing at all, which is exactly what a kernel-level fault looks like from
the BMC's perspective. Three parallel unthrottled migrations had run 3.5 h
beforehand with no trouble.

ntfs-3g is FUSE/userspace: a fault there kills a process, not the kernel.
TrueNAS ships no ntfs-3g, and the Debian 13 VM's binary needs GLIBC_2.38
(TrueNAS has 2.36). The working build came from the Debian 12 laptop, which
matches the NAS's glibc exactly. It lives in NTFS3G_DIR below.

Mount (read-only, always):
  LD_LIBRARY_PATH=<dir> <dir>/ntfs-3g -o ro,streams_interface=none \
      /dev/<resolved-by-serial>3 /mnt/WindowsDrive

ALWAYS resolve the device by SERIAL. Letters reshuffle across reboots -- this
drive has been sdg, sdk, sdi and sdh, and the boot SSDs have moved into those
same letters. A letter-based operation would eventually hit the wrong disk.

Copies with rsync (never cp: cp chmods each directory it creates, which fails
EPERM on aclmode=restricted datasets and makes cp abort the subtree while
still exiting 0 -- a silent no-op). Verifies count + size + checksum sample.
Never writes to or wipes the source.
"""
import hashlib
import os
import re
import random
import subprocess
import sys
import time

SRC = "/mnt/WindowsDrive"
DST = "/mnt/Cloud36/Fileshare/Recovered-from-sdg"
SERIAL = "SERIAL0004"
NTFS3G_DIR = "/mnt/Cloud36/Fileshare/Services/JARVIS/ntfs3g"

# Keep-list confirmed by owner 2026-08-27. Explicitly NOT kept: MeshRoom
# (136 GB), Dynmap (8.3 GB), Plex Metadata (38 GB), ssd backup (40 GB of old
# Minecraft servers + 2020-era MAC OS/PiHole VM images), Windows OS, Recovery.
ITEMS = [("DFL", "DFL"), ("ks backup", "ks-backup-full"), ("Users/OWNER", "Users-OWNER")]

BWLIMIT_KBPS = int(sys.argv[sys.argv.index("--bwlimit") + 1]) \
    if "--bwlimit" in sys.argv else 20000


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def resolve_device():
    for d in sorted(f"/dev/{x}" for x in os.listdir("/dev")
                    if len(x) == 3 and x.startswith("sd")):
        try:
            out = subprocess.run(["smartctl", "-i", d], capture_output=True,
                                 text=True, timeout=30).stdout
        except subprocess.SubprocessError:
            continue
        for line in out.splitlines():
            if "Serial Number" in line and SERIAL in line:
                return d
    return None


def ensure_mounted():
    if os.path.ismount(SRC):
        with open("/proc/mounts") as fh:
            for line in fh:
                if SRC in line:
                    if "fuseblk" not in line:
                        log(f"!! {SRC} is mounted with the KERNEL driver, not "
                            f"ntfs-3g -- unmount it first: {line.strip()}")
                        return False
                    log(f"already mounted (fuseblk): {line.split()[0]}")
                    return True
    dev = resolve_device()
    if not dev:
        log(f"could not find a disk with serial {SERIAL}")
        return False
    log(f"resolved {SERIAL} -> {dev} (partition {dev}3)")
    os.makedirs(SRC, exist_ok=True)
    env = dict(os.environ, LD_LIBRARY_PATH=NTFS3G_DIR)
    r = subprocess.run([f"{NTFS3G_DIR}/ntfs-3g", "-o",
                        "ro,streams_interface=none", f"{dev}3", SRC],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        log(f"mount failed: {(r.stderr or r.stdout).strip()[:300]}")
        return False
    log("mounted read-only via ntfs-3g (fuseblk)")
    return True


RSYNC_TMP = re.compile(r"^\..*\.[A-Za-z0-9]{6}$")


def sweep_rsync_temps(dst_root, src_root):
    """Remove rsync's orphaned in-flight files ('.<name>.XXXXXX') left by a
    hard kill -- this box crashed twice mid-rescue. They appear as EXTRA
    files and fail the count check even though every real file copied.

    CRITICAL: the name pattern alone is NOT sufficient. It matched real files
    -- '.vs/slnx.sqlite' fits it exactly, because 'sqlite' is six characters
    -- and an earlier pattern-only sweep deleted two genuine files. A file is
    only a temp if it ALSO has no counterpart in the source tree.
    """
    n = 0
    for dp, _, fs in os.walk(dst_root):
        for f in fs:
            if not RSYNC_TMP.match(f):
                continue
            rel = os.path.relpath(os.path.join(dp, f), dst_root)
            if os.path.exists(os.path.join(src_root, rel)):
                continue          # exists in source -> a real file, keep it
            try:
                os.remove(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
    if n:
        log(f"  swept {n} orphaned rsync temp file(s)")
    return n


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


def main():
    if not ensure_mounted():
        sys.exit(1)
    for src_rel, dst_name in ITEMS:
        src, dst = os.path.join(SRC, src_rel), os.path.join(DST, dst_name)
        log(f"=== {src_rel} -> {dst_name} ===")
        if not os.path.exists(src):
            log("  source missing, skipping")
            continue
        if os.path.exists(dst):
            log("  partial destination -- rsync will resume")
        os.makedirs(dst, exist_ok=True)

        sm = tree(src)
        log(f"  source: {len(sm)} files, {sum(sm.values())/1e9:.1f} GB")
        t0 = time.time()
        r = subprocess.run(
            ["ionice", "-c", "2", "-n", "7", "rsync", "-rt", "--no-perms",
             "--no-owner", "--no-group", "--partial",
             f"--bwlimit={BWLIMIT_KBPS}",
             src.rstrip("/") + "/", dst.rstrip("/") + "/"],
            capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  RSYNC rc={r.returncode}: {(r.stderr or '').strip()[-300:]}")
            continue
        el = time.time() - t0
        log(f"  copied in {el/60:.1f} min ({sum(sm.values())/1e6/max(el,1):.0f} MB/s)")

        sweep_rsync_temps(dst, src)
        dm = tree(dst)
        if len(sm) != len(dm):
            log(f"  !! FILE COUNT MISMATCH {len(sm)} vs {len(dm)} -- review")
            continue
        bad = [k for k in sm if sm.get(k) != dm.get(k)]
        if bad:
            log(f"  !! SIZE MISMATCH on {len(bad)} files, e.g. {bad[:3]}")
            continue
        rels = list(sm)
        sample = rels if len(rels) <= 10 else random.sample(rels, 10)
        mism = [rel for rel in sample
                if md5(os.path.join(src, rel)) != md5(os.path.join(dst, rel))]
        if mism:
            log(f"  !! CHECKSUM MISMATCH {mism}")
            continue
        log(f"  VERIFIED: {len(sm)} files, {len(sample)}-file checksum sample OK")
    log("rescue complete -- source mounted read-only throughout, never modified")


if __name__ == "__main__":
    main()
