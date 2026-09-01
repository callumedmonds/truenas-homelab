# Cold media tier — design

Two-tier media storage. Hot tier: `Cloud36` (RAIDZ2, redundant) for actively
watched and high-bitrate content. Cold tier: cheap single drives, no
redundancy, for rarely-watched content that can simply be re-downloaded if a
drive dies.

## Selection criteria (locked)

- **Ranking, not cutoff**: order all media by `view_count ASC, last_watched
  ASC, size DESC` (views aggregated across all Plex accounts) and demote from
  the top of that queue until the cold tier reaches its fill cap. Never-watched
  content naturally sorts first.
- **Bitrate cap: ≤ 50 Mbps.** Basis: measured worst-case sequential read on the
  first cold drive is 64 MB/s ≈ 513 Mbps (inner tracks); 4 concurrent 50 Mbps
  streams = 200 Mbps leaves ~60% margin for seek degradation. Higher-bitrate
  items (4K remuxes) stay on the redundant pool regardless of views.
- **TV granularity: whole series.** Sonarr models one folder per series and
  cannot split one across root folders, but natively moves series between root
  folders with files (`MoveSeries`). A series ranks as one unit.
- **Fill cap: 85% per cold drive** (~790 GB on a 1 TB drive). ZFS allocation
  slows badly past ~90%. Fleet rule: keep enough aggregate free space across
  cold drives to absorb the largest one, so a failing drive can be evacuated.
- **No prefetch.** The 50 Mbps cap makes next-episode playback from cold
  indistinguishable from hot. Instead: **nightly promotion** — any cold series
  with new watch activity is moved back to hot as a whole series via Sonarr.
- **Never move a title that's still genuinely seeding.** Excluded from the
  candidate queue outright, not just deferred — no "untrack then move"
  workaround. A title becomes eligible again once qBittorrent shows it's no
  longer seeding (naturally finished, or the user's own decision to stop it —
  not something the migration process does on its own). See "Current
  seeding state" below for how to tell genuine seeds from dead registry
  entries.

## Architecture (locked)

**mergerfs unified view on the VM.** Merge `/srv/plex-media` (hot NFS) +
`/srv/cold01` (+ future `/srv/coldNN`) into one tree, bind-mounted into the
Plex container at `/share` — the container path Plex has always used. Because
the folder structure is mirrored across tiers (`Movies/`, `Series/`), a file's
Plex-visible path is **identical regardless of which drive holds it**. Watch
history is structurally immune to migration: Plex cannot see that a file moved.

Sonarr / Radarr / qBittorrent (TrueNAS apps) work on **real per-tier root
folders**, not the merged view — new downloads hardlink into the hot tier
(same filesystem, seed-friendly), and the arrs move items between tiers with
their own databases staying correct.

## Current state

All three cold pools are single-disk, no redundancy, `recordsize=1M`,
`atime=off`, lz4, NFSv4 ACLs (owner@ + group@ + `group:apps(568)` +
`group:Callum(1000)`, all FULL_CONTROL inheritable), NFS-exported to
192.0.2.0/24 with `maproot=root`, mounted on the VM, in the mergerfs
union, and added as root folders in both Sonarr and Radarr. jarvis-heal picks
them up from fstab on its own.

| Pool | Disk | Serial | Size | Notes |
|---|---|---|---|---|
| `cold01` | WDC WD10JPVX | SERIAL0001 | 1 TB | first cold drive |
| `cold02` | Samsung HD502HJ | SERIAL0002 | 500 GB | ~17k power-on hrs, SMART clean |
| `cold03` | Seagate ST6000DM003 | SERIAL0003 | 6 TB | **SMR** — see warning below |

**`cold03` is an SMR (shingled) drive.** Sustained large sequential writes
slow badly once its CMR cache fills, which is exactly the migration and
drive-evacuation workload. Reads are unaffected, so it's fine as a
mostly-read cold target, but: benchmark real sustained write throughput
before relying on it for a time-critical evacuation, and prefer CMR drives
(`cold01`/`cold02`) as the destination when evacuating a failing drive.
The 50 Mbps bitrate cap was derived from `cold01`'s CMR read profile and
has **not** been re-validated against `cold03`.

- The 1 TB HGST (serial SERIAL0004, currently `/dev/sdk` — it re-letters
  between boots, always identify by serial) still holds the old Windows
  install and is **not** part of the cold tier. Its keep-list (ks backup
  remainder, MeshRoom, DFL) still needs rescuing to Cloud36 first.
- `cold02` previously held a 2020–2021 Plex library (~435 GB). Wiped
  2026-08-27 on the owner's instruction. Most of it was incomplete
  uTorrent downloads (`.!ut`); ~15 complete 4K/UHD titles were lost with
  it, all re-downloadable.

## Current seeding state (measured 2026-08-27)

qBittorrent's registry is mostly dead weight: of 309 registered torrents,
280 (90%) are orphaned — Sonarr/Radarr already imported and renamed the
file, and the original raw-named download folder is simply gone. Those
carry no seeding obligation at all.

Of the 29 torrents whose folder still exists, **20 are fully downloaded and
actively seeding** (2,116 hardlinked files, 1,648 GB) — these are the ones
the exclusion rule above applies to. The other 9 are still mid-download and
were never migration candidates in the first place. All tracker hosts seen
are public/open trackers (no ratio requirement), so there's no cost to a
title becoming eligible the moment it naturally stops seeding — just re-run
the same census to see what's dropped off the live list.

## Add-a-drive runbook (repeat per drive)

1. Confirm the drive by **serial**, wipe intent confirmed by owner.
2. TrueNAS: create single-disk pool `coldNN`, dataset `coldNN/media`
   (recordsize 1M, atime off), `Movies/` + `Series/`, chown 1000:1000.
3. NFS-export `/mnt/coldNN/media` to 192.0.2.0/24.
4. VM: fstab entry → `/srv/coldNN` (nfs4, `_netdev,x-systemd.automount`),
   mount. Nothing to add to jarvis-heal — it reads the NFS entries out of
   fstab, so a new tier is covered the moment fstab is.
5. Add `/srv/coldNN` as a mergerfs branch; Plex needs no change at all.
6. Sonarr/Radarr: add the tier's root folder paths.

## Evacuate-a-drive runbook

1. Stop demotions to the sick drive.
2. Move its content to other cold branches (rsync or arr root-folder moves) —
   the merged view, and therefore Plex, is unchanged throughout.
3. Remove the branch from mergerfs, retire the pool.

## Operational gotchas (found during the sacrificial-title proof)

- **mergerfs caches attributes.** A permission/ownership change made on the
  NFS side (e.g. an ACL fix) is not always picked up live — `umount
  /srv/media-all && mount /srv/media-all` on the VM forces a fresh read.
- **The Plex container pins the mount instance it was started with.** Docker
  bind-mounts a specific mount, not a path — if you remount `/srv/media-all`
  on the VM host *after* the container is already running, the container
  keeps seeing the old (now-orphaned) instance and reports files as missing
  even though they're really there. `docker restart jarvis-plex` (or
  `compose up -d plex`) after any host-side remount fixes it.
  **Rule of thumb: touched `/srv/media-all` on the host? Restart jarvis-plex
  afterward, every time — new drive added, ACL changed, branch removed.**

## Status

Implementation done and proven:
- mergerfs union live at `/srv/media-all` (`/srv/plex-media` + `/srv/cold*`
  glob branch — add a drive, no fstab edit needed).
- Plex's `/share` remapped to it in `docker-compose.yml` (was
  `/srv/plex-media` directly).
- Sonarr/Radarr/qBittorrent host + all indexers repointed to the post-move
  LAN; import pipeline proven live end-to-end.
- cold01 converted to NFSv4 ACLs mirroring the warm tier + mounted into the
  Sonarr/Radarr containers (they had no path to it at all before this).
- Warm tier's missing `owner@` ACE fixed recursively (uid 1000 — which is
  what Plex Media Server actually runs as — was denied by FUSE
  `default_permissions` despite NFS granting access via group ACL).
- Plex auto-empty-trash + media-deletion disabled (mergerfs defeats the
  "location unavailable" safety net; this was the last thing standing
  between a missing branch and mass-deleted library entries).
- **Sacrificial-title proof passed** (2026-08-27): moved *Thadam (2019)*
  (watched once) from hot to cold01. Post-move DB state — `metadata_items.id`,
  `media_parts.id`, file path, `view_count`, `last_viewed_at` — is byte-for-byte
  identical to the pre-move baseline. Plex cannot detect the branch change.

Remaining before bulk migration: none identified. The 20 titles still
genuinely seeding are simply excluded from the candidate queue (see
Selection criteria) — no per-title qBittorrent housekeeping needed.
