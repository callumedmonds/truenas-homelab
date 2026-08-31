#!/bin/sh
# Start fan_control.py if it is not already running. Idempotent, so the same
# command serves as both the POSTINIT boot hook and the cron restart-watchdog.
#
# The watchdog matters more here than it did for the migration. fan_control.py
# forces fans to 0xff on every exit path, so a clean death is loud but safe --
# but it leaves the box at full speed indefinitely until something restarts
# the daemon. A SIGKILL, or the host OOMing it, skips the failsafe entirely
# and strands the fans at whatever duty was last written. Five-minute cron
# bounds both cases.
#
# Not a systemd unit: /etc/systemd/system is writable on TrueNAS but is lost
# on upgrade (upgrades build a new boot environment). Init scripts and cron
# live in the config database and survive both. Same reasoning as
# cold-spindown-launch.sh and cold-migrate-launch.sh.
#
# Registered as: POSTINIT COMMAND -> sh '<this path>'
# Cron: every 5 min, same command

SCRIPTS=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
DAEMON="$SCRIPTS/fan_control.py"
LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/fan-control-launch.log

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null
}

# Match the python process itself, not any shell that merely mentions the
# name. A bare "fan_control" pattern also matches an admin's own ssh or grep,
# and the launcher then wrongly concludes it is already running -- the
# self-match trap that cost real debugging time on the migration scripts.
if pgrep -f "python3 .*fan_control\.py" >/dev/null 2>&1; then
    exit 0
fi

# Wait for the pool holding the script to be mounted. POSTINIT fires after
# pool import, but be defensive: a missing script here would silently mean no
# fan control until the next cron tick.
i=0
while [ ! -f "$DAEMON" ] && [ $i -lt 30 ]; do
    sleep 10
    i=$((i + 1))
done
if [ ! -f "$DAEMON" ]; then
    log "daemon not found at $DAEMON after 300s -- giving up this cycle"
    exit 1
fi

nohup python3 -u "$DAEMON" --interval 30 >> /var/log/fan-control.out 2>&1 &
log "started fan_control.py pid=$!"
exit 0
