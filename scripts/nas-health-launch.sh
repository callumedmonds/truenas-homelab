#!/bin/sh
# Start nas_health.py if it is not already running. Idempotent, so the same
# command works as the POSTINIT boot hook and the cron restart-watchdog.
#
# Not a systemd unit: /etc/systemd/system is writable on TrueNAS but is lost
# on upgrade (upgrades build a new boot environment). Init scripts and cron
# live in the config database and survive both. Same reasoning as the other
# launchers here.
#
# Started as a *transient* systemd unit via launch-lib.sh, never with nohup:
# under cron's sudo a nohup'd daemon loses the ability to exec anything. See
# launch-lib.sh. Transient units live in /run, so the point above still holds.
#
# Kept separate from nas_metrics.py on purpose: fan control depends on that
# endpoint, so a fault in the health endpoint must not be able to take it down.
#
# Registered as: POSTINIT COMMAND -> sh '<this path>'
# Cron: every 5 min, same command

SCRIPTS=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
DAEMON="$SCRIPTS/nas_health.py"
LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/nas-health-launch.log
PORT=9102

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null
}

# Match the python process itself, not any shell that merely mentions the
# name -- a bare "nas_health" pattern also matches an admin's own ssh or
# grep, and the launcher would then wrongly decide it is already running.
if pgrep -f "python3 .*nas_health\.py" >/dev/null 2>&1; then
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

[ -f "$SCRIPTS/launch-lib.sh" ] || { log "launch-lib.sh missing from $SCRIPTS -- not starting"; exit 1; }
. "$SCRIPTS/launch-lib.sh"
how=$(start_daemon nas-health /var/log/nas-health.out \
    /usr/bin/python3 -u "$DAEMON" --port "$PORT")
log "started nas_health.py on port ${PORT} $how"
exit 0
