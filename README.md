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
