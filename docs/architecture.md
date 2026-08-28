# Architecture notes

## Network

- NAS: static `192.0.2.10/24` on bridge `br0`, gateway/DNS `192.0.2.1` +
  `1.1.1.1`. `br0` sits on `bond0` = eno1–eno4, **balance-xor (layer2+3)** —
  the switch (TP-Link TL-SG1024D) is unmanaged, so LACP is impossible; xor
  spreads outbound by destination. All internet-bound traffic hashes to one
  link (router = one MAC); only LAN clients spread across the four.
- VM (`DebianServ`): static `192.0.2.20/24` on `ens3` via NetworkManager
  (`ipv4.may-fail no`). It was on DHCP, which raced boot: DHCP timed out,
  `network-online.target` was reached with no address, the NFS mounts failed
  ("Network is unreachable"), and every container binding them exited 255.
  Happened after both the house move and a power cut.
- The VM's traffic reaches the LAN through `br0` → `bond0`, so Plex already
  benefits from the link aggregation.
- Tailscale runs as a TrueNAS app: node `truenas-scale` (100.x.y.z),
  approved subnet router for `192.0.2.0/24` and exit node. This replaced a
  subnet route advertised by an offline Home Assistant box, which blackholed
  the whole LAN for any device with `accept-routes` on.

## Storage

- `Cloud36`: 6×6 TB RAIDZ2 (sda–sdf) + Intel Optane NVMe log. Survives two
  simultaneous disk failures.
- `boot-pool`: mirror of two 120 GB Kingston A400s (sdi/sdj), both ~5 years
  old; sdj has 4 historical uncorrectable errors but passed a full extended
  SMART test. Replace as a pair. A TrueNAS config backup makes boot-drive
  death a restore, not a rebuild: System → General → Manage Configuration.
- `cold01`: see cold-tier.md.

## Known fragilities / lessons

- **No UPS.** Every serious incident here (corrupted monerod LMDB, two rounds
  of dead NFS mounts + downed containers, 440/810 unsafe-shutdown counts on
  the boot SSDs) traces to unclean power loss.
- TrueNAS network changes: use `interface.commit` with `rollback: true` and a
  checkin window, and confirm from the box itself — the middleware restart
  takes ~70 s, longer than the default 60 s window.
- NFS exports carry network ACLs (`sharing.nfs.query` → `networks`) — they
  broke silently when the LAN was renumbered onto a different subnet.
- Plex's SQLite DB uses a custom collation (`icu_root`); the `sqlite3` CLI
  cannot open it. Python's `sqlite3` with a dummy collation registered works.
  Also: during a library scan its WAL exceeds 200 MB and a **read-only** open
  fails with "file is not a database" (RO cannot replay a WAL, worse over
  NFS). Snapshot db + `-wal` + `-shm` to local disk and read the copy.
- **`cp` is unsafe on datasets with `aclmode=restricted`.** `cp -a` (and even
  `cp -r`) chmods each directory it creates; that fails EPERM and cp aborts
  the whole subtree **while still exiting 0**. It silently copies nothing.
  Always `rsync --no-perms --no-owner --no-group`. Found when a 60 GB rescue
  "succeeded" in 0 seconds having copied 0 of 156,687 files.
- **Never identify a disk by letter.** Letters reshuffle across reboots: the
  old Windows drive has been `sdg`, `sdk`, and `sdi`, and the boot SSDs moved
  `sdi`/`sdj` → `sdk`/`sdl`. Wiping "sdk" from yesterday's notes would have
  destroyed a boot SSD. Match on serial (`smartctl -i`).

## Crash investigation, 2026-08-27 (two hard crashes)

Both crashes: kernel log stops mid-line, no panic/oops/MCE, ~2 min gap, then
auto-restart. All ZFS pools came back healthy with zero data errors.

Ruled OUT:
- **PSU/power.** Dual redundant Platinum PSUs; live rails healthy (3.3V 3.31,
  5V 4.99, 12V 11.98), `Main Power Fault: false`, no overload.
- **Thermal shutdown.** No critical-temp event, and a thermal cutoff powers
  off rather than hanging.
- **Load alone.** Three unthrottled parallel migrations ran 3.5 h fine.

Prime suspect: **the `ntfs3` kernel driver.** Both crashes came minutes after
an NTFS read of the old Windows drive began (5 min, then 15 min) — including
the second one at a throttled 20 MB/s with drives at 43-48 °C. A kernel-level
fault is invisible to the BMC, which matches the empty SEL. TrueNAS has no
`ntfs-3g`; **the Debian VM does** (userspace FUSE, cannot panic the host), so
NTFS reads belong there, not on the NAS.

The BMC event log was **100 % full** (512 entries; 500 voltage events from a
single May 2025 burst), so it had recorded nothing for over a year. Cleared
2026-08-27, history saved to `Cloud36/Fileshare/Services/JARVIS/diagnostics/`.
**Check `ipmitool sel list` first after any future incident.**

## Cooling

Marginal and worth fixing independently of the crashes. BMC fan mode is
already Full Speed, yet only 2 of 8 headers are populated, at 2100 RPM — an
SC846 mid-wall normally runs 3+ fans at 4000-8000 RPM. `sda` idles ~49 °C
against a 40 °C reported threshold, with a 64 °C lifetime max and 23
over-temperature-limit events. Ideal for these drives is 35-45 °C.
Noctua industrialPPC-3000s are the planned fix.
