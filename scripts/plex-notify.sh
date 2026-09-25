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
# so these calls are accepted unauthenticated.
#
# Sonarr/Radarr set eventtype=Test when you press Test in the UI.
#
# THE SECTION MUST MATCH THE PATH. This script used to hardcode SECTION=1
# ("Films") on the assumption that it was the only library. It is not -- there
# is also section 2 ("TV Programmes", /share/Series), so every Sonarr import
# asked Plex to refresh a /share/Series/... path inside the *movie* section.
# Plex answers that with HTTP 200 and then discards it:
#   ERROR - '/share/Series/X' was not inside any known section location, skipping.
# wget sees 200 and the old script logged "refresh sent OK", so ~340 TV
# refreshes silently did nothing. The library only stayed current because a
# nightly scan happened to cover for it.
#
# So: resolve the section from the imported path by matching it against each
# section's own <Location path="...">, and refuse to send a request we can
# already tell Plex will throw away.

# Set in the Sonarr/Radarr container environment, e.g. PLEX_HOST=192.0.2.20:32400.
# Kept out of the repo because it is a real address on a real LAN.
PLEX_HOST="${PLEX_HOST:?PLEX_HOST not set -- see scripts/homelab.env.example}"
LOG="${PLEX_NOTIFY_LOG:-/config/plex-notify.log}"

log() { echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

plex_get() { wget -q -T 10 -O - "http://${PLEX_HOST}$1" 2>/dev/null; }

# Plex returns one <Directory> per section, each followed by its <Location>s.
# Split on '<' so every tag is its own line, then walk them in order.
sections_xml() { printf '%s' "$SECTIONS" | tr '<' '\n'; }

# Section whose Location is $1 or a parent directory of it.
resolve_by_path() {
    sections_xml | awk -v folder="$1" '
        /^Directory / {
            key = ""
            if (match($0, /key="[0-9]+"/)) key = substr($0, RSTART+5, RLENGTH-6)
        }
        /^Location / {
            p = ""
            if (match($0, /path="[^"]*"/)) p = substr($0, RSTART+6, RLENGTH-7)
            # Compare on a path boundary so /share/Movies does not swallow
            # /share/MoviesOld.
            if (p != "" && key != "" && (folder == p || index(folder, p "/") == 1)) {
                print key; exit
            }
        }'
}

# Section of content type $1 ("show" / "movie"), for when we have no path.
resolve_by_type() {
    sections_xml | awk -v want="$1" '
        /^Directory / {
            key = ""; t = ""
            if (match($0, /key="[0-9]+"/)) key = substr($0, RSTART+5, RLENGTH-6)
            if (match($0, /type="[a-z]+"/)) t = substr($0, RSTART+6, RLENGTH-7)
            if (t == want && key != "") { print key; exit }
        }'
}

urlencode() {
    printf '%s' "$1" | sed \
        -e 's/%/%25/g' -e 's/ /%20/g' -e 's/#/%23/g' -e 's/&/%26/g' \
        -e "s/'/%27/g" -e 's/+/%2B/g' -e 's/,/%2C/g' -e 's/:/%3A/g' \
        -e 's/;/%3B/g' -e 's/?/%3F/g' -e 's/\[/%5B/g' -e 's/\]/%5D/g'
}

# Which arr called us decides the content type we fall back to.
if [ -n "${sonarr_eventtype:-}" ]; then
    EVENT="$sonarr_eventtype"; FOLDER="${sonarr_series_path:-}"; WANT_TYPE=show
elif [ -n "${radarr_eventtype:-}" ]; then
    EVENT="$radarr_eventtype"; FOLDER="${radarr_movie_path:-}"; WANT_TYPE=movie
else
    EVENT=unknown; FOLDER=""; WANT_TYPE=""
fi

# Events worth a rescan. Upgrades arrive as Download with sonarr_isupgrade=True
# -- there is no separate "Upgrade" eventtype, so do not list one.
case "$EVENT" in
  Test)
    SECTIONS=$(plex_get /library/sections)
    if [ -z "$SECTIONS" ]; then log "test FAILED -- no response from $PLEX_HOST"; exit 1; fi
    S=$(resolve_by_type "$WANT_TYPE")
    if [ -z "$S" ]; then
        log "test FAILED -- Plex has no '$WANT_TYPE' section to refresh"; exit 1
    fi
    log "test OK -- $WANT_TYPE content maps to section $S"
    exit 0
    ;;
  Download|Rename|SeriesAdd|SeriesDelete|EpisodeFileDelete|MovieAdded|MovieFileDelete|MovieDelete)
    ;;
  *)
    log "ignoring event: $EVENT"; exit 0
    ;;
esac

SECTIONS=$(plex_get /library/sections)
if [ -z "$SECTIONS" ]; then
    log "FAILED ($EVENT) -- could not read sections from $PLEX_HOST"; exit 1
fi

SECTION=""
if [ -n "$FOLDER" ]; then
    SECTION=$(resolve_by_path "$FOLDER")
    if [ -n "$SECTION" ]; then
        URL="/library/sections/${SECTION}/refresh?path=$(urlencode "$FOLDER")"
        DESC="targeted refresh ($EVENT) section $SECTION: $FOLDER"
    else
        # Plex would answer 200 and discard this, so say so instead of
        # pretending it worked, and fall back to the whole section.
        log "no section owns '$FOLDER' -- check the library's Location paths"
    fi
fi

if [ -z "$SECTION" ]; then
    SECTION=$(resolve_by_type "$WANT_TYPE")
    if [ -z "$SECTION" ]; then
        log "FAILED ($EVENT) -- no '$WANT_TYPE' section in Plex, nothing refreshed"
        exit 1
    fi
    URL="/library/sections/${SECTION}/refresh"
    DESC="full-section refresh ($EVENT) section $SECTION ($WANT_TYPE)"
fi

log "$DESC"
if wget -q -T 20 -O /dev/null "http://${PLEX_HOST}${URL}"; then
    log "refresh sent OK"
    exit 0
fi

log "refresh FAILED, retrying full section $SECTION"
if wget -q -T 20 -O /dev/null "http://${PLEX_HOST}/library/sections/${SECTION}/refresh"; then
    log "fallback OK"
else
    log "fallback FAILED"
fi
exit 0
