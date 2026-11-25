#!/bin/bash
# deploy/linkgraph-watch.sh with a fake repo (watch.py prints a set line and may leave a kick), fake gh, fake notify.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); fails=0; n=0
mkdir -p "$T/repo/scripts" "$T/state"
cat > "$T/repo/scripts/watch.py" <<'PY'
import os, sys
kick = sys.argv[sys.argv.index("--kick-file") + 1]
if os.environ.get("FAKE_KICK") == "1":
    open(kick, "w").write("x 51 of 52\n")
print(os.environ.get("FAKE_LINE", "ok: 52 of 52"))
PY
cat > "$T/gh" <<'SH'
#!/bin/bash
echo "$@" >> "$FAKE_GH_LOG"; exit ${FAKE_GH_RC:-0}
SH
cat > "$T/notify" <<'SH'
#!/bin/bash
echo "$@" >> "$FAKE_NOTIFY_LOG"
SH
chmod +x "$T/gh" "$T/notify"
export LG_WATCH_STATE="$T/state" LG_WATCH_REPO="$T/repo" LG_WATCH_GH="$T/gh" LG_WATCH_NOTIFY="$T/notify"
export FAKE_GH_LOG="$T/gh.log" FAKE_NOTIFY_LOG="$T/notify.log"
run(){ : > "$T/gh.log"; : > "$T/notify.log"; bash "$HERE/deploy/linkgraph-watch.sh" > "$T/out" 2>&1; }
check(){ n=$((n+1)); if eval "$2"; then :; else echo "FAIL $1"; cat "$T/out"; fails=$((fails+1)); fi; }
FAKE_KICK=1 FAKE_LINE="ok: 51 of 52" run
check "kick file starts the build once" 'grep -q "workflow run build.yml" "$T/gh.log" && [ -e "$T/state/linkgraph-watch.kicked" ] && [ ! -e "$T/state/linkgraph-watch.kick" ]'
FAKE_KICK=1 FAKE_LINE="ok: 51 of 52" run
check "a second kick within 3 h does not start another build" '[ ! -s "$T/gh.log" ]'
rm -f "$T/state/linkgraph-watch.kicked"
FAKE_KICK=1 FAKE_GH_RC=1 FAKE_LINE="ok: 51 of 52" run
check "a failed start pages" 'grep -q "starting the build failed" "$T/notify.log" && [ ! -e "$T/state/linkgraph-watch.kicked" ]'
FAKE_KICK=0 FAKE_LINE="ok: 52 of 52" run
check "no kick file, no build" '[ ! -s "$T/gh.log" ]'
FAKE_KICK=0 FAKE_LINE="link history: behind Common Crawl and nothing added for 14 h" run
check "stale still pages" 'grep -q "nothing added" "$T/notify.log"'
echo "$n checks, $fails failed"; rm -rf "$T"; exit $((fails>0))
