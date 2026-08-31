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

# cold03 ONLY, deliberately. cold01, cold02 and cold04 all dropped off the
# HBA with DID_NO_CONNECT on 2026-08-31 and needed a physical reseat; a
# spin-up is exactly when a marginal backplane contact fails, so they stay
# spinning until that is properly fixed. cold03 has never faulted, is the
# largest cold drive (so the biggest power win), and sits on a different
# HBA target group from the three that did. See cold_idle_spindown.py.
POOLS=cold03

# 60 rather than 30: cold03 holds the bulk of the migrated library (4.5 TB,
# ~195 titles), so it is the pool most likely to see a stray Plex read
# shortly after going quiet. A longer window trades a little idle power for
# noticeably fewer spin-up cycles on a 31,000-hour drive.
IDLE_MINUTES=60
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
    --only "$POOLS" >> /var/log/cold-spindown.out 2>&1 &
log "started cold_idle_spindown.py (only=${POOLS} idle=${IDLE_MINUTES}min interval=${INTERVAL}s) pid=$!"
exit 0
