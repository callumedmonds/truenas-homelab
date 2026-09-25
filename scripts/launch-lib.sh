# Shared by the *-launch.sh scripts. Sourced, not executed:
#     . "$SCRIPTS/launch-lib.sh"
#
# start_daemon UNIT OUTFILE COMMAND [ARGS...]
#   Starts COMMAND as the transient systemd unit UNIT.service, appending its
#   stdout/stderr to OUTFILE. Prints how it was started, for the caller's log
#   line. Falls back to nohup if systemd-run refuses.
#
# WHY NOT NOHUP
# -------------
# TrueNAS runs cron jobs through `sudo`, and sudoers here has
# `Defaults log_subcmds` (intercept mechanism "trace"). That puts every
# descendant of the cron job under a seccomp filter whose exec checks are
# answered by that sudo process. Once the cron job returns and sudo exits,
# nothing answers, and every exec in anything left running fails with ENOSYS
# ("Function not implemented"). A nohup'd daemon started by a cron watchdog
# can therefore never run ipmitool, zpool, hdparm or smartctl again.
#
# POSTINIT is unaffected (middlewared runs init scripts without sudo), which
# is why daemons started at boot always worked and this stayed hidden until
# a watchdog actually restarted one. Found 2026-09-25 when
# nas_power_push.py silently stopped reading the BMC. To check a running
# daemon: `grep Seccomp /proc/<pid>/status` should say 0, not 2.
#
# A unit started by PID 1 inherits none of that, whoever runs the launcher.
# Transient units live in /run, so nothing is written to /etc/systemd (lost
# on upgrade), and the POSTINIT + cron registration stays the source of
# truth. No Restart= policy: the cron watchdogs remain the restart mechanism,
# as before.

start_daemon() {
    _unit=$1
    _out=$2
    shift 2
    # --collect unloads the unit once it exits, and reset-failed clears any
    # leftover failed state, so the same unit name can always be reused.
    systemctl reset-failed "$_unit.service" >/dev/null 2>&1
    if systemd-run --unit="$_unit" --collect --quiet \
            -p StandardOutput="append:$_out" -p StandardError="append:$_out" \
            "$@" >/dev/null 2>&1; then
        echo "as transient unit $_unit.service"
        return 0
    fi
    nohup "$@" >> "$_out" 2>&1 &
    echo "with nohup pid=$! (systemd-run failed -- under cron's sudo this daemon cannot exec)"
    return 0
}
