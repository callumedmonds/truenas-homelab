#!/bin/bash
# Ensure NAS NFS mounts are present and crashed jarvis containers are restarted.
# Only restarts containers that exited NON-ZERO (a crash), never ones stopped deliberately.
NAS=192.0.2.10
log(){ logger -t jarvis-heal "$*"; }

for i in $(seq 1 60); do
  timeout 3 bash -c "echo > /dev/tcp/$NAS/2049" 2>/dev/null && break
  sleep 5
done

for m in /srv/jarvis-data /srv/plex-media /srv/cold01; do
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
