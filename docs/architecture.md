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
