#!/bin/bash
# Hourly on Layag (cron "23 * * * *"): is the public link history (ahon-digital/linkgraph) keeping up?
# Pages via diwa-notify at most every 6 h while unhealthy, and once on recovery.
# Self-heal (26 Nov): when watch.py leaves a kick file (behind Common Crawl, nothing queued or running, no run in
# the last 3 h, not failing), start the build workflow, at most once per 3 h. A failed start pages, it never hides.
# Installed copy: /opt/scripts/linkgraph-watch.sh. Watcher code pinned at /opt/ahon-linkgraph (not auto-pulled).
set -uo pipefail
STATE=${LG_WATCH_STATE:-/var/lib}
STAMP=$STATE/linkgraph-watch.paged
KICK=$STATE/linkgraph-watch.kick
KICKED=$STATE/linkgraph-watch.kicked
REPO_DIR=${LG_WATCH_REPO:-/opt/ahon-linkgraph}
NOTIFY=${LG_WATCH_NOTIFY:-"python3 /opt/ahonos/brain/scripts/misc/diwa-notify.py --subsystem linkgraph --product ahonseo"}
GH=${LG_WATCH_GH:-gh}
export AHONOS_HOME=/opt/ahonos
rm -f "$KICK"
msg=$(cd "$REPO_DIR" && python3 -B scripts/watch.py --kick-file "$KICK" 2>&1 | tr "\n" " " | cut -c1-600)
echo "$(date -u +%FT%TZ) $msg"
if [ -e "$KICK" ]; then
  if [ -z "$(find "$KICKED" -mmin -180 2>/dev/null)" ]; then
    if $GH workflow run build.yml -R ahon-digital/ahon-linkgraph >/dev/null 2>&1; then
      touch "$KICKED"; echo "$(date -u +%FT%TZ) kicked the build: $(cat "$KICK")"
    else
      $NOTIFY --severity warn -m "link history: behind and idle, and starting the build failed (gh workflow run)"
    fi
  else
    echo "$(date -u +%FT%TZ) kick wanted, but one was sent under 3 h ago"
  fi
  rm -f "$KICK"
fi
if echo "$msg" | grep -q "^ok:"; then
  if [ -e "$STAMP" ]; then $NOTIFY --severity info -m "link history recovered: ${msg#ok: }" && rm -f "$STAMP"; fi
  exit 0
fi
if [ ! -e "$STAMP" ] || [ -n "$(find "$STAMP" -mmin +360 2>/dev/null)" ]; then
  $NOTIFY --severity warn -m "$msg" && touch "$STAMP"
fi
