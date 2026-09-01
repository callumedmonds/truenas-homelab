#!/bin/bash
# Ensure NAS NFS mounts are present and crashed jarvis containers are restarted.
# Only restarts containers that exited NON-ZERO (a crash), never ones stopped deliberately.
# Site values live outside the repo (serials and addresses identify one
# machine). systemd passes NAS_IP via EnvironmentFile; when run by hand, fall
# back to /etc/homelab.env. See scripts/homelab.env.example.
[ -r /etc/homelab.env ] && . /etc/homelab.env
NAS="${NAS_IP:?NAS_IP not set -- see scripts/homelab.env.example}"
log(){ logger -t jarvis-heal "$*"; }

for i in $(seq 1 60); do
  timeout 3 bash -c "echo > /dev/tcp/$NAS/2049" 2>/dev/null && break
  sleep 5
done

# Which mounts to heal comes from fstab, not a list kept here. That list had
# already drifted once: the copy running on the VM carried cold02..cold04 that
# this file never gained, so restoring from this repo would have quietly
# stopped healing three of them -- during a rebuild, which is exactly when
# nobody is checking. fstab is what mount(8) consults anyway, so it cannot
# disagree with the machine.
mapfile -t MOUNTS < <(awk '$1 !~ /^#/ && $3 ~ /^nfs/ { print $2 }' /etc/fstab)
[ "${#MOUNTS[@]}" -gt 0 ] || { log "no nfs entries in /etc/fstab; nothing to heal"; exit 0; }

for m in "${MOUNTS[@]}"; do
  if ! mountpoint -q "$m"; then
    systemctl reset-failed "$(systemd-escape -p --suffix=mount "$m")" 2>/dev/null
    mount "$m" 2>/dev/null && log "mounted $m" || log "FAILED to mount $m"
  fi
done

mountpoint -q /srv/jarvis-data || { log "jarvis-data still absent; not touching containers"; exit 0; }

for c in $(docker ps -a --filter "name=jarvis-" --format '{{.Names}}'); do
  running=$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
  code=$(docker inspect -f '{{.State.ExitCode}}' "$c" 2>/dev/null)
  policy=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c" 2>/dev/null)
  if [ "$running" != "true" ] && [ "$code" != "0" ] && [ "$policy" = "unless-stopped" ]; then
    docker start "$c" >/dev/null 2>&1 && log "restarted crashed container $c (exit $code)"
  fi
done
