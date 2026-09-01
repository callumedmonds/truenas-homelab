#!/bin/sh
# Tell Plex to rescan after Sonarr/Radarr imports something.
#
# Wired in as a "Custom Script" connection in both arrs. Replaces Plex's
# daily full-library scan, which walked every branch of the mergerfs union
# and woke the spun-down cold drives once a day for nothing -- cold content
# never changes except when we deliberately migrate it.
#
# NO AUTH TOKEN NEEDED: Plex's Preferences.xml has
#   allowedNetworks="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
# which covers both the LAN and the docker bridge networks the arrs sit on,
# so these calls are accepted unauthenticated. Verified: /library/sections
# and /library/sections/1/refresh both return 200 from inside the container.
#
# Scans only the imported folder where the arr tells us which one it was
# (sonarr_series_path / radarr_movie_path), falling back to a full-section
# refresh. A targeted scan avoids touching cold branches entirely.
#
# Sonarr/Radarr set eventtype=Test when you press Test in the UI.

# Set in the Sonarr/Radarr container environment, e.g. PLEX_HOST=192.0.2.20:32400.
# Kept out of the repo because it is a real address on a real LAN.
PLEX_HOST="${PLEX_HOST:?PLEX_HOST not set -- see scripts/homelab.env.example}"
SECTION=1                      # "Films" -- the only section; covers both trees
LOG=/config/plex-notify.log

log() { echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

EVENT="${sonarr_eventtype:-${radarr_eventtype:-unknown}}"
FOLDER="${sonarr_series_path:-${radarr_movie_path:-}}"

case "$EVENT" in
  Test)
    log "test event -- probing Plex"
    if wget -q -T 10 -O /dev/null "http://${PLEX_HOST}/library/sections"; then
      log "test OK"; exit 0
    fi
    log "test FAILED"; exit 1
    ;;
  Download|Rename|Upgrade|MovieFileDelete|EpisodeFileDelete|MovieAdded|SeriesAdd)
    ;;
  *)
    log "ignoring event: $EVENT"; exit 0
    ;;
esac

if [ -n "$FOLDER" ]; then
    # URL-encode: spaces and the handful of chars that actually appear in
    # media folder names. Plex wants the path it sees, which is the same
    # /share/... path thanks to the mirrored tier layout.
    ENC=$(printf '%s' "$FOLDER" | sed \
        -e 's/%/%25/g' -e 's/ /%20/g' -e 's/#/%23/g' -e 's/&/%26/g' \
        -e "s/'/%27/g" -e 's/+/%2B/g' -e 's/,/%2C/g' -e 's/:/%3A/g' \
        -e 's/;/%3B/g' -e 's/?/%3F/g' -e 's/\[/%5B/g' -e 's/\]/%5D/g')
    URL="http://${PLEX_HOST}/library/sections/${SECTION}/refresh?path=${ENC}"
    log "targeted refresh ($EVENT): $FOLDER"
else
    URL="http://${PLEX_HOST}/library/sections/${SECTION}/refresh"
    log "full-section refresh ($EVENT) -- no folder in env"
fi

if wget -q -T 20 -O /dev/null "$URL"; then
    log "refresh sent OK"
else
    log "refresh FAILED, retrying full section"
    wget -q -T 20 -O /dev/null "http://${PLEX_HOST}/library/sections/${SECTION}/refresh" \
        && log "fallback OK" || log "fallback FAILED"
fi
exit 0
