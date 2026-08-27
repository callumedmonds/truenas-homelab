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

- `cold01` = 1 TB WD (serial SERIAL0001), pool ONLINE, dataset
  `cold01/media` (recordsize=1M, atime=off, lz4), `Movies/` + `Series/`
  owned 1000:1000, NFS-exported to 192.0.2.0/24, mounted at `/srv/cold01`
  on the VM, covered by jarvis-heal.
- Second drive (`cold02`) will be the 1 TB HGST currently holding an old
  Windows install — only after its keep-list is rescued to Cloud36.

## Add-a-drive runbook (repeat per drive)

1. Confirm the drive by **serial**, wipe intent confirmed by owner.
2. TrueNAS: create single-disk pool `coldNN`, dataset `coldNN/media`
   (recordsize 1M, atime off), `Movies/` + `Series/`, chown 1000:1000.
3. NFS-export `/mnt/coldNN/media` to 192.0.2.0/24.
4. VM: fstab entry → `/srv/coldNN` (nfs4, `_netdev,x-systemd.automount`),
   add to jarvis-heal mount list, mount.
5. Add `/srv/coldNN` as a mergerfs branch; Plex needs no change at all.
6. Sonarr/Radarr: add the tier's root folder paths.

## Evacuate-a-drive runbook

1. Stop demotions to the sick drive.
2. Move its content to other cold branches (rsync or arr root-folder moves) —
   the merged view, and therefore Plex, is unchanged throughout.
3. Remove the branch from mergerfs, retire the pool.

## Status

Implementation plan (mergerfs setup, compose remap, sacrificial-title proof of
watch-history survival, bulk migration order) — in progress.
