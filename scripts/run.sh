#!/usr/bin/env bash
# One build step: fold the next Common Crawl release (in date order) into the link history on Hugging Face.
# Exits 0 without work when the history is current. Needs HF_TOKEN, LG_REPO; optional LG_WORK (scratch dir),
# LG_SHARD_KB (history shard size, default 524288 = 512 MB), LG_DRY=1 (build everything, upload nothing).
# Tests only: LG_CC_BASE (another source, e.g. file:///...), LG_RELEASE_LIST (a fixed list instead of the page).
# Logs carry release names, counts, sizes and timings only.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
LG="$here/../lg"
W=${LG_WORK:-/tmp/lg-work}
SHARD_KB=${LG_SHARD_KB:-524288}
CC=${LG_CC_BASE:-https://data.commoncrawl.org/projects/hyperlinkgraph}
# these paths are pasted into commands lg runs; plain characters only
for p in "$here" "$W"; do case "$p" in *[!A-Za-z0-9/_.-]*) echo "unsafe path: $p"; exit 1;; esac; done
mkdir -p "$W"; cd "$W"
log() { echo "== $(date -u +%FT%TZ) $*"; }

log "release list"
if [ -n "${LG_RELEASE_LIST:-}" ]; then cp "$LG_RELEASE_LIST" all.txt; else python3 "$here/releases.py" > all.txt; fi
# Where the history stands, from all three pieces at once. They must agree; a fresh start is allowed only when
# none exists. One missing piece next to the others is a broken dataset, never "start over".
st() { local rc=0; "$@" || rc=$?; [ "$rc" = 0 ] || [ "$rc" = 3 ] || { echo "cannot read the dataset's state"; exit 1; }; echo "$rc"; }
rel_rc=$(st python3 "$here/hf.py" get releases.tsv done.tsv)
idx_rc=$(st python3 "$here/hf.py" get history/history.idx prev.idx)
dom_rc=$(st python3 "$here/hf.py" exists domains.txt)
for v in "$rel_rc" "$idx_rc" "$dom_rc"; do case "$v" in 0|3) ;; *) echo "cannot read the dataset's state: stop"; exit 1;; esac; done
# Exactly two valid states: nothing at all (fresh start), or all three present and agreeing. We never write an
# empty release log, so an empty one is as broken as a missing one.
if [ "$rel_rc" = 3 ] && [ "$idx_rc" = 3 ] && [ "$dom_rc" = 3 ]; then
  # a fresh start rewrites everything, so it needs a second, independent "empty" from another endpoint
  fr=0; python3 "$here/hf.py" fresh || fr=$?
  [ "$fr" = 0 ] || { echo "listing says empty but the repo holds data (or cannot tell): stop"; exit 1; }
  : > done.tsv
elif [ "$rel_rc" = 0 ] && [ "$idx_rc" = 0 ] && [ "$dom_rc" = 0 ]; then
  [ -s done.tsv ] || { echo "release log is empty next to an existing history: stop"; exit 1; }
else
  echo "dataset is partial (release log $rel_rc, history $idx_rc, domains $dom_rc; 0 = present): stop"; exit 1
fi
n_done=$(grep -c . done.tsv || true)
awk -F'\t' -v n="$n_done" '$1 != NR - 1 { bad = 1 } END { exit bad || NR != n }' done.tsv || { echo "release log is not numbered 0..n-1: stop"; exit 1; }
if [ "$n_done" -gt 0 ]; then
  # checked on every run, before "nothing to do", without downloading the big files: the whole index parses and
  # names the last logged release, and the domain list's recorded hash and size are the ones the log wrote
  [ "$("$LG" checkidx prev.idx)" = "$((n_done - 1))" ] || { echo "history index and release log disagree: stop"; exit 1; }
  [ "$(python3 "$here/hf.py" domsha)" = "$(tail -1 done.tsv | cut -f6,7)" ] || { echo "domains.txt is not the list the last release recorded: stop"; exit 1; }
fi
# the history must be a prefix of Common Crawl's order; anything else stops the build instead of guessing
cut -f2 done.tsv > done_names.txt
awk -v n="$n_done" 'NR <= n' all.txt | cmp -s - done_names.txt || { echo "release log is not a prefix of Common Crawl's order: stop"; exit 1; }
R=$(sed -n "$((n_done + 1))p" all.txt)
if [ -z "$R" ]; then log "history is current ($n_done releases); nothing to do"; exit 0; fi
RI=$n_done
[ "$RI" -le 254 ] || { echo "release index past 254: the format needs a wider field"; exit 1; }
log "next: $R (index $RI)"

B="$CC/$R/domain/$R"
curl -sSf --retry 5 --retry-delay 30 "$B-domain.properties" -o props.txt
N=$(sed -n 's/^nodes=//p' props.txt); E=$(sed -n 's/^arcs=//p' props.txt)
# inbound peaks at 20 bytes a node while counting (positions, release map, in-degrees and offsets live together),
# then holds 16 bytes a node plus 4 bytes a link of one part. When all links do not fit, it places them in parts,
# streaming the edges once more per part (each pass ~15 min on 2025-size releases). A domain with more links than a
# part gets a part of its own, paid from the 1.5 GB headroom; inbound reports that size. Running out of memory kills
# the run before anything is published.
fixed=$(( 20 * (N + 1) ))
avail=$(awk '/MemAvailable/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$avail" -gt 0 ]; then room=$(( (avail - 1500000000 - fixed) / 4 )); else room=$E; fi
[ "$room" -gt 0 ] || { echo "not enough memory for $N nodes ($((avail / 1000000)) MB available)"; exit 1; }
# Parts are cut at domain boundaries, so each is filled up to the room (an even split spills into an extra part).
if [ "$room" -ge "$E" ]; then PARTS=1; PART=$E; else PARTS=$(( (E + room - 1) / room )); PART=$room; fi
[ "$PARTS" -le "${LG_MAX_PARTS:-6}" ] || { echo "would need $PARTS passes over the links ($((avail / 1000000)) MB available): stop"; exit 1; }
log "nodes $N links $E, memory about $(( (fixed + 4 * PART) / 1000000 )) MB in $PARTS part(s) of $PART links"

log "names"
curl -sSf --retry 5 --retry-delay 30 "$B-domain-vertices.txt.gz" | gzip -dc | "$LG" names rel_names.txt rel_pos.u32
sha() { python3 -c 'import hashlib,sys; h=hashlib.sha256(); f=open(sys.argv[1],"rb"); [h.update(c) for c in iter(lambda: f.read(1<<22), b"")]; print(h.hexdigest())' "$1"; }
if [ "$RI" = 0 ]; then OLD=-; else
  python3 "$here/hf.py" get domains.txt old_domains.txt
  # the domain list must be the very one the last release wrote: its hash and line count are in the release log
  want=$(tail -1 done.tsv | cut -f6,7)
  got="$(sha old_domains.txt)	$(wc -c < old_domains.txt | tr -d ' ')"
  [ "$want" = "$got" ] || { echo "domains.txt is not the list the last release recorded: stop"; exit 1; }
  OLD=old_domains.txt
fi
"$LG" dictmerge "$OLD" rel_names.txt domains.txt map_old.u32 map_rel.u32
rm -f rel_names.txt old_domains.txt

# With more than one part, the counting pass keeps a binary copy of the links (~5 bytes each) and the placing passes
# read it: one download and one text parse instead of one per pass. Only when the disk holds it and the output.
CACHE=""
if [ "$PARTS" -gt 1 ]; then
  disk=$(df -Pk . | awk 'NR==2 {print $4 * 1024}')
  if [ "$disk" -gt $(( 10 * E + 5000000000 )) ]; then CACHE=edges.cache; fi
fi
log "inbound: ${PARTS} part(s), placing passes read ${CACHE:-the edge download again}"
"$LG" inbound "$N" "$E" map_rel.u32 rel_pos.u32 rel.bin "curl -sSf --retry 5 --retry-delay 30 '$B-domain-edges.txt.gz' | gzip -dc" "$PART" ${CACHE:+"$CACHE"}
rm -f edges.cache
rm -f map_rel.u32 rel_pos.u32

log "merge into history"
# the dataset is created only here: past the "nothing to do" exit and never on a dry run (shards upload during merge)
[ "${LG_DRY:-0}" = 1 ] || python3 "$here/hf.py" ensure
rm -rf hist manifest.tsv; mkdir hist; : > manifest.tsv
if [ "$RI" = 0 ]; then PREV=-; PREVIDX=-; else
  PREVIDX=prev.idx
  PREV="python3 '$here/hf.py' cat $(grep -v '^#' prev.idx | cut -f1 | uniq | awk '{printf "history/h-%05d.bin ", $1}')"
fi
if [ "${LG_DRY:-0}" = 1 ]; then HOOK="true"; else HOOK="sh -c 'python3 \"$here/hf.py\" put \"\$0\" history/\$(basename \"\$0\") manifest.tsv && rm -f \"\$0\"'"; fi
"$LG" merge "$RI" "$PREV" "$PREVIDX" map_old.u32 rel.bin hist "$SHARD_KB" "$HOOK" | tee merge.json
rm -f map_old.u32 rel.bin

if [ "${LG_DRY:-0}" = 1 ]; then log "dry run: built $(ls hist | wc -l) files, uploaded nothing"; du -sh hist; exit 0; fi
log "publish"
cp done.tsv releases.tsv
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$RI" "$R" "$N" "$E" "$(date -u +%FT%TZ)" "$(sha domains.txt)" "$(wc -c < domains.txt | tr -d ' ')" >> releases.tsv
python3 "$here/hf.py" put hist/history.idx history/history.idx manifest.tsv
python3 "$here/hf.py" put domains.txt domains.txt manifest.tsv
python3 "$here/hf.py" put domains.txt.idx domains.txt.idx manifest.tsv
python3 "$here/hf.py" put releases.tsv releases.tsv manifest.tsv
python3 "$here/hf.py" put "$here/../dataset/README.md" README.md manifest.tsv
python3 "$here/hf.py" commit "add $R (release $RI)" manifest.tsv --delete-missing history/
python3 "$here/hf.py" squash
touch "$W/did_work"
log "done: $R"
