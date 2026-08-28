#!/bin/sh
# Wait for the NTFS rescue to finish, then build cold04 -- but only if the
# rescued keep-list verifies. make_cold04.py re-checks every item (file count,
# every file's size, checksum sample) and exits non-zero without touching the
# disk if anything fails, so this chain cannot wipe data that wasn't saved.
#
# Owner authorised the wipe: "once data is copied off cold4 wipe it and fill
# it" (2026-08-27). This script enforces the "once data is copied off" half.
#
# The VM-side mount (/srv/cold04 + mergerfs branch + arr bind mount) is NOT
# done here -- the NAS has no SSH key to the VM. Run that separately.
D=/mnt/Cloud36/Fileshare/Services/JARVIS/migration-scripts
LOG=/tmp/cold04_chain.log

echo "$(date '+%F %T') waiting for rescue_ntfs.py to finish..." >> "$LOG"
while pgrep -f rescue_ntfs.py >/dev/null 2>&1; do
    sleep 60
done
echo "$(date '+%F %T') rescue process gone -- verifying" >> "$LOG"

python3 "$D/make_cold04.py" --verify-only >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date '+%F %T') VERIFICATION FAILED -- cold04 NOT created, disk untouched" >> "$LOG"
    exit 1
fi

echo "$(date '+%F %T') verified -- creating cold04" >> "$LOG"
python3 "$D/make_cold04.py" --yes >> "$LOG" 2>&1
echo "$(date '+%F %T') chain finished rc=$?" >> "$LOG"
