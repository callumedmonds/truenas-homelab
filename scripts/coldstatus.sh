#!/bin/sh
# One-shot status summary for the cold-tier migration.
#
# Lives as a script rather than an inline ssh command specifically so that
# pgrep patterns like "coldmig" do not appear in the *invoking* shell's own
# command line. An inline `ssh truenas '... pgrep -f coldmig ...'` makes the
# remote shell itself match the pattern, inflating every process count and (in
# the launchers) causing false "already running" results. Invoked as
# `sh coldstatus.sh`, this script's cmdline contains none of the patterns.

STATE=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
DIAG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics

echo "UPTIME: $(uptime | sed 's/^ *//')"

echo "POOLS:"
zfs list -H -o name,used,avail cold03/media 2>/dev/null | sed 's/^/  /'
df -h /mnt/Cloud36/Fileshare 2>/dev/null | tail -1 | sed 's/^/  /'

echo "PROCS:"
for pat in coldmig thermal_throttle cold_idle_spindown; do
    n=$(pgrep -c -f "python3 -u .*${pat}" 2>/dev/null || echo 0)
    printf "  %-20s %s\n" "$pat" "$n"
done

echo "PROGRESS:"
LOG=/var/log/coldmig-cold03.out
[ -s "$LOG" ] || LOG=/tmp/cold03.log
printf "  completed titles: %s\n" "$(grep -c 'Path ->' "$LOG" 2>/dev/null || echo 0)"
printf "  current: %s\n" "$(grep -a '=== ' "$LOG" 2>/dev/null | tail -1)"
[ -f "$STATE/cold03.done" ] && echo "  *** MIGRATION COMPLETE: $(cat "$STATE/cold03.done")"

echo "ERRORS:"
grep -aiE 'MISMATCH|FAILED|SAFETY GUARD|RSYNC rc=' "$LOG" 2>/dev/null | tail -5 | sed 's/^/  /' \
    || true
grep -aqiE 'MISMATCH|FAILED|SAFETY GUARD|RSYNC rc=' "$LOG" 2>/dev/null || echo "  none"

echo "TEMPS:"
tail -1 "$DIAG/temps.log" 2>/dev/null | sed 's/^/  /'

echo "DRIVES:"
printf "  "
for d in sdg sdh sdi sdj; do
    printf "%s=%s " "$d" "$(hdparm -C /dev/$d 2>/dev/null | grep -i 'drive state' | sed 's/.*: *//')"
done
echo

echo "WATCHDOG:"
tail -2 "$DIAG/migrate-launch.log" 2>/dev/null | sed 's/^/  /' || echo "  (no launches logged)"
