#!/bin/sh
# Re-assert BMC-side settings that do NOT survive a BMC restart.
#
# IPMI "Set Sensor Event Enable" (NetFn 0x04, Cmd 0x28) is volatile by spec:
# the BMC restores every sensor's SDR default whenever its firmware restarts.
# `ipmitool mc reset cold`, pulling both PSU cords, and a BMC firmware update
# all undo anything set here -- which is why this runs from cron as well as at
# boot rather than being applied once by hand.
#
# Registered as: POSTINIT COMMAND -> sh '<this path>'
# Cron: every 15 min, same command (idempotent -- no-ops when already correct)

LOG=/mnt/Cloud36/Fileshare/Services/JARVIS/diagnostics/bmc-assert.log

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null
}

# --- Chassis intrusion, sensor 0x51 --------------------------------------
# The lid switch on this chassis is physically broken and asserts "General
# Chassis intrusion" permanently.
#
# Not cosmetic: the SEL on this box has been full once already (500 voltage
# events from a single May 2025 burst filled it, so nothing was recorded for
# over a year -- including two hard crashes in Aug 2026 that we then had no
# data for). A sensor that logs continuously destroys the event history this
# box depends on for diagnosis. It is also the reason PSU visibility via
# JPI2C1 was worth wiring up, and that log space now has a real job to do.
#
# Byte 2 = 0x80:  [7:6]=10b  leave the individual event enables alone
#                 [5]=0      disable all event messages from this sensor
#                 [4]=0      disable sensor scanning
#
# The permanent fix is a shunt across JL1 (a closed switch reads "lid on").
# This stays regardless: it costs nothing and covers the case where the
# header is disturbed again.
state=$(ipmitool sdr elist 2>/dev/null | grep -i intru)
case "$state" in
    "")
        log "intrusion sensor absent from SDR -- skipping"
        ;;
    *Disabled*)
        ;;                                  # already off, nothing to do
    *"No Reading"*)
        # The BMC is reporting the sensor as "Device Not Present". It is inert
        # in this state -- not asserting, not logging -- and the BMC actively
        # REJECTS Set Sensor Event Enable for it (completion code 0xcb,
        # "Requested sensor, data, or record not found"). So there is nothing
        # to disable and nothing wrong: treat it as a quiet success rather
        # than retrying and logging a failure every 15 minutes.
        #
        # A cold BMC reset drops the sensor into this state. Refitting the lid
        # can bring it back, which is exactly when the branch below matters.
        ;;
    *)
        err=$(ipmitool raw 0x04 0x28 0x51 0x80 2>&1)
        if [ $? -eq 0 ]; then
            log "disabled chassis intrusion sensor 0x51 (was:$(echo "$state" | tr -s ' ' | cut -d'|' -f3-))"
        else
            log "FAILED to disable sensor 0x51: $(echo "$err" | tr -s ' \n' ' ')"
        fi
        ;;
esac

exit 0
