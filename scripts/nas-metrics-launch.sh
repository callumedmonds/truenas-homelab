#!/bin/sh
# Start nas_metrics.py if it is not already running. Idempotent, so the same
# command works as the POSTINIT boot hook and the cron restart-watchdog.
#
# Not a systemd unit: /etc/systemd/system is writable on TrueNAS but is lost
# on upgrade (upgrades build a new boot environment). Init scripts and cron
# live in the config database and survive both. Same reasoning as the other
# launchers here.
#
# Registered as: POSTINIT COMMAND -> sh '<this path>'
# Cron: every 5 min, same command

SCRIPTS=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
DAEMON="$SCRIPTS/nas_metrics.py"
LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/nas-metrics-launch.log
PORT=9101

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null
}

# Match the python process itself, not any shell that merely mentions the
# name -- a bare "nas_metrics" pattern also matches an admin's own ssh or
# grep, and the launcher would then wrongly decide it is already running.
if pgrep -f "python3 .*nas_metrics\.py" >/dev/null 2>&1; then
    exit 0
fi

# Wait for the pool holding the script to be mounted. POSTINIT fires after
# pool import, but be defensive.
i=0
while [ ! -f "$DAEMON" ] && [ $i -lt 30 ]; do
    sleep 10
    i=$((i + 1))
done
if [ ! -f "$DAEMON" ]; then
    log "daemon not found at $DAEMON after 300s -- giving up this cycle"
    exit 1
fi

nohup python3 -u "$DAEMON" --port "$PORT" >> /var/log/nas-metrics.out 2>&1 &
log "started nas_metrics.py on port ${PORT} pid=$!"
exit 0
