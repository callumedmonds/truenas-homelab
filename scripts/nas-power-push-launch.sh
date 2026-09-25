#!/bin/sh
# Start nas_power_push.py if it is not already running. Idempotent, so the
# same command works as the POSTINIT boot hook and the cron restart-watchdog.
#
# Not a systemd unit: /etc/systemd/system is lost on TrueNAS upgrade (upgrades
# build a new boot environment). Init scripts and cron live in the config
# database and survive both. Same reasoning as the other launchers here.
#
# Started as a *transient* systemd unit via launch-lib.sh, never with nohup:
# under cron's sudo a nohup'd daemon loses the ability to exec anything. See
# launch-lib.sh. Transient units live in /run, so the point above still holds.
#
# Registered as: POSTINIT COMMAND -> sh '<this path>'
# Cron: every 5 min, same command

SCRIPTS=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
DAEMON="$SCRIPTS/nas_power_push.py"
LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/nas-power-push-launch.log

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null
}

# Match the python process itself, not any shell that merely mentions the
# name (an admin's own ssh or grep would otherwise count as "running").
if pgrep -f "python3 .*nas_power_push\.py" >/dev/null 2>&1; then
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

# Without its token the daemon can only fail against HA every few seconds, so
# don't start it. HA_TOKEN_FILE comes from homelab.env.
TOKEN_FILE=$(sed -n 's/^HA_TOKEN_FILE="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$SCRIPTS/homelab.env" 2>/dev/null)
if [ -z "$TOKEN_FILE" ] || [ ! -s "$TOKEN_FILE" ]; then
    log "HA token file '${TOKEN_FILE:-unset}' missing or empty -- not starting"
    exit 1
fi

[ -f "$SCRIPTS/launch-lib.sh" ] || { log "launch-lib.sh missing from $SCRIPTS -- not starting"; exit 1; }
. "$SCRIPTS/launch-lib.sh"
how=$(start_daemon nas-power-push /var/log/nas-power-push.out \
    /usr/bin/python3 -u "$DAEMON")
log "started nas_power_push.py $how"
exit 0
