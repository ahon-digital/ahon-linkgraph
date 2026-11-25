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
TIMEOUT=${LG_WATCH_TIMEOUT-timeout 120}   # a stalled gh never holds the lock past the next hourly run
export AHONOS_HOME=/opt/ahonos
# One run at a time, by a KERNEL file lock (Codex r1: overlapping runs both dispatched; r2: a stale-lock takeover
# let two in). flock(2) is held by this shell's open file description on fd 9 and released by the kernel when the
# run ends or crashes, so there is no stale lock to take over. perl takes it (present on Linux and macOS; util-linux
# flock(1) is not on macOS). A run that cannot get the lock logs and exits; the next hour runs.
exec 9>>"$STATE/linkgraph-watch.lock"
if ! perl -e 'use Fcntl qw(:flock); open(my $f, ">&=9") or exit 2; flock($f, LOCK_EX | LOCK_NB) or exit 1'; then
  echo "$(date -u +%FT%TZ) another watch run holds the lock; skipped"; exit 0
fi
rm -f "$KICK"
msg=$(cd "$REPO_DIR" && python3 -B scripts/watch.py --kick-file "$KICK" 2>&1 | tr "\n" " " | cut -c1-600)
echo "$(date -u +%FT%TZ) $msg"
if [ -e "$KICK" ]; then
  if [ -z "$(find "$KICKED" -mmin -180 2>/dev/null)" ]; then
    if $TIMEOUT $GH workflow run build.yml -R ahon-digital/ahon-linkgraph >/dev/null 2>&1; then
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
