#!/bin/sh
# Launcher for cold_idle_spindown.py. Idempotent: safe to run repeatedly, so
# it works both as a POSTINIT boot hook and as a periodic restart-on-crash
# check from cron.
#
# Why not a systemd unit: /etc/systemd/system is writable on TrueNAS, but a
# unit placed there is lost on upgrade (upgrades create a new boot
# environment). Init/Shutdown Scripts live in the TrueNAS config database and
# survive both reboots and upgrades. The existing truenas-net-assert.sh entry
# follows the same pattern.
#
# Also re-asserts hddstandby on the cold drives: the middleware applies it via
# hdparm at system.ready, but a reboot can reorder device letters, and the
# setting is per-disk-identifier in the config DB rather than something the
# kernel keeps.
#
# Registered as: POSTINIT COMMAND -> sh '<this path>'
# Cron: every 15 min, same command (idempotent, so it just no-ops when healthy)

SCRIPTS=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
DAEMON="$SCRIPTS/cold_idle_spindown.py"
LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/spindown-launch.log
IDLE_MINUTES=30
INTERVAL=120

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null
}

# Already running? Nothing to do. pgrep -f on the full path avoids matching
# an unrelated python3, and avoids the self-match trap that plain pkill -f
# patterns fall into.
if pgrep -f "python3 .*cold_idle_spindown\.py" >/dev/null 2>&1; then
    exit 0
fi

# Wait for the pool holding the script to actually be mounted. POSTINIT fires
# after pool import, but be defensive -- a missing script here would silently
# mean no spindown until the next cron tick.
i=0
while [ ! -f "$DAEMON" ] && [ $i -lt 30 ]; do
    sleep 10
    i=$((i + 1))
done
if [ ! -f "$DAEMON" ]; then
    log "daemon not found at $DAEMON after 300s -- giving up"
    exit 1
fi

nohup python3 -u "$DAEMON" --idle-minutes "$IDLE_MINUTES" --interval "$INTERVAL" \
    >> /var/log/cold-spindown.out 2>&1 &
log "started cold_idle_spindown.py (idle=${IDLE_MINUTES}min interval=${INTERVAL}s) pid=$!"
exit 0
