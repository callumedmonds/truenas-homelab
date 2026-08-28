#!/bin/sh
# Resume the cold-tier migration after a crash or reboot. Idempotent, so it
# works as both a POSTINIT boot hook and a periodic cron watchdog.
#
# This box has crashed twice mid-migration. Nothing was lost -- coldmig.py
# verifies every copy before deleting its source, and rsync --partial resumes
# interrupted files -- but the migration simply STOPPED until restarted by
# hand. That's the gap this closes: after an auto-reboot the work picks up on
# its own.
#
# Safe to run repeatedly:
#   * exits if a migration for this pool is already running
#   * exits if <pool>.done exists (that run finished; cmd_plan clears markers)
#   * coldmig.py itself skips titles whose source is already gone
#
# Not a systemd unit: /etc/systemd/system is writable on TrueNAS but is lost
# on upgrade (new boot environment). Init scripts and cron live in the config
# database and survive both.

POOL=${1:-cold03}
SCRIPTS=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
STATE=$SCRIPTS
LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/migrate-launch.log

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') [$POOL] $*" >> "$LOG" 2>/dev/null
}

# Finished already?
[ -f "$STATE/$POOL.done" ] && exit 0

# Already running? (match the pool argument so two pools can run separately)
# Match the python process itself. A bare "coldmig.py run $POOL"
# pattern also matches any shell whose command line merely mentions
# it -- including an admin's own ssh/grep -- and the launcher then
# wrongly concludes it is already running.
if pgrep -f "python3 .*coldmig\.py run $POOL" >/dev/null 2>&1; then
    exit 0
fi

# Wait for the pool holding the scripts + plan to be mounted.
i=0
while { [ ! -f "$SCRIPTS/coldmig.py" ] || [ ! -f "$STATE/plan.json" ]; } && [ $i -lt 30 ]; do
    sleep 10
    i=$((i + 1))
done
if [ ! -f "$SCRIPTS/coldmig.py" ] || [ ! -f "$STATE/plan.json" ]; then
    log "scripts or plan not available after 300s -- giving up this cycle"
    exit 1
fi

# Destination pool must actually be imported, or every title fails.
if ! zpool list "$POOL" >/dev/null 2>&1; then
    log "pool $POOL not imported -- skipping this cycle"
    exit 1
fi

# Thermal guard alongside it (it exits on its own when no worker is running).
if ! pgrep -f "python3 .*thermal_throttle\.py" >/dev/null 2>&1; then
    nohup python3 -u "$SCRIPTS/thermal_throttle.py" \
        --bw-start 200000 --bw-max 200000 --bw-min 20000 \
        >> /var/log/thermal-throttle.out 2>&1 &
    log "started thermal_throttle.py pid=$!"
fi

nohup python3 -u "$SCRIPTS/coldmig.py" run "$POOL" --bwlimit 200000 \
    >> "/var/log/coldmig-$POOL.out" 2>&1 &
log "started coldmig.py run $POOL pid=$!"
exit 0
