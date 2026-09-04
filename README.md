# truenas-homelab

Infrastructure config and runbooks for the home TrueNAS SCALE server (`truenas`,
192.0.2.10) and its Debian VM (`DebianServ`, 192.0.2.20) which runs Plex
and the jarvis docker stack.

## Layout

| Path | What |
|---|---|
| `scripts/jarvis-heal.sh` | Self-healing: remounts NAS NFS shares and restarts crashed `jarvis-*` containers. Lives at `/usr/local/bin/jarvis-heal.sh` on the VM. |
| `systemd/jarvis-heal.service` + `.timer` | Runs the heal script 90s after boot and every 5 min. `/etc/systemd/system/` on the VM. |
| `docs/architecture.md` | Current network + storage architecture and why it is this way. |
| `docs/cold-tier.md` | The two-tier media storage design: criteria, mergerfs plan, runbooks. |

## The machines

- **truenas** — TrueNAS SCALE 25.10.1. Pool `Cloud36`: 6×6 TB RAIDZ2 + NVMe log.
  Pool `cold01`: single 1 TB drive, no redundancy (deliberate — re-downloadable
  media only). Apps: qBittorrent, Syncthing, Sonarr, Radarr, Jackett, Tailscale
  (subnet router + exit node for 192.0.2.0/24).
- **DebianServ** — VM on the NAS (Debian 13). Docker: Plex (`jarvis-plex`),
  Prowlarr, Ollama, Redis, Postgres, jarvis frontend/backend. Media arrives
  over NFS from the NAS. Static IP (DHCP raced the NFS mounts at boot and
  killed Plex twice — see docs/architecture.md).

## Why jarvis-heal exists

Two real incidents: an unclean shutdown (house move) and a power cut both left
the VM up with its NFS mounts failed, so every container bind-mounting them
exited 255 and stayed down. The heal timer remounts and restarts only
containers that *crashed* (non-zero exit, restart policy `unless-stopped`) —
never ones stopped deliberately.

## When a cold pool dies, the whole server stalls

Found the hard way. A cold-tier pool is a **single disk with no redundancy**, so
losing that disk suspends the pool outright (`state: SUSPENDED`, "devices are
faulted in response to IO failures"). What is not obvious is how far the damage
spreads:

1. A suspended pool blocks IO indefinitely — `txg_sync` parks in `D` state and
   `/proc/pressure/io` shows `full` around 50-70%, i.e. the machine is fully
   IO-blocked most of the time.
2. `nfsd` threads serving that pool's export block with it. The thread pool is
   finite, so once enough are stuck **every other NFS export stops responding
   too** — including exports from perfectly healthy pools.
3. The VM's NFS mounts are `hard` by default, meaning IO to them blocks
   *forever, uninterruptibly*. Anything touching them wedges in `D` state and
   cannot be killed; load climbs and the VM stops answering pings.

So it presents as "the VM dropped off the network". It has not: check
`vnet0` in `ip -s link` — **zero dropped packets** means the network path is
fine and the host is simply too stalled to reply. `failmode=continue` on the
pool is not enough on its own; writes already in flight still block.

A suspended pool also blocks **middleware config changes**: `midclt call
nfs.update` fails with `[EFAULT] [EZFS_POOLUNAVAIL]`, so some fixes cannot be
applied until the pool is recovered.

**Mitigations**

- Mount the cold tier `soft` on the VM (`soft,timeo=100,retrans=3`) so IO fails
  with an error after ~30s instead of blocking forever. Keep the pool holding
  Postgres/appdata `hard` — soft mounts can return errors mid-write, which that
  data should not tolerate.
- Raise the nfsd thread count so one sick export cannot starve the rest:
  `midclt call nfs.update '{"servers": 128}'` (only works once no pool is
  suspended; `echo 128 > /proc/fs/nfsd/threads` applies it immediately but does
  not survive a reboot).
- Mirror the cold pools if the data on them actually matters. Everything above
  is damage limitation for an unredundant pool by design.

**Recovery**: the disk must be physically back before ZFS can clear the fault —
`DID_NO_CONNECT` in the kernel log means cable, power, backplane or a dead
drive. Reconnect, confirm it reappears in `lsblk`, then `zpool clear <pool>`.
The VM will need a reboot afterwards; its `D`-state processes cannot be killed.

## Configuration

Addresses and drive serials are **not** in this repo. They identify one
specific machine — a drive serial is what warranty and RMA lookups key on, and
it correlates this repo with anything else quoting the same hardware — so they
live in a gitignored `homelab.env`:

```bash
cp scripts/homelab.env.example scripts/homelab.env
$EDITOR scripts/homelab.env          # your serials, from: lsblk -o NAME,SERIAL,MODEL,SIZE
```

Deploy it alongside the scripts. The Python scripts read it through
`homelab_env.py` (no path setup needed — python puts a script's own directory
first on `sys.path`); the shell scripts source it, or take the same variables
from the environment. Anything already exported wins over the file, so a
systemd `EnvironmentFile` or a container env var overrides any key.

Every address in the docs below is an RFC 5737 documentation address
(`192.0.2.x`), not a real one.

## Restoring after a rebuild

```bash
scp scripts/jarvis-heal.sh root@192.0.2.20:/usr/local/bin/
scp systemd/jarvis-heal.* root@192.0.2.20:/etc/systemd/system/
ssh root@192.0.2.20 'chmod +x /usr/local/bin/jarvis-heal.sh && systemctl daemon-reload && systemctl enable --now jarvis-heal.timer'
```
