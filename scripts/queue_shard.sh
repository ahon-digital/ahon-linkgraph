#!/bin/sh
# lg merge's shard hook: hand a finished shard to scripts/uploader.py (which uploads several at once) and return, so
# the merge writes the next shard while earlier ones upload. Waits while UPQ_MAX shards are queued (bounded disk).
# Refuses (non-zero, which stops the merge) once an upload failed or the uploader is gone.
set -eu
q="$UPQ"
while :; do
  [ -e "$q/FAILED" ] && { echo "queue_shard: a shard upload failed; stopping the merge" >&2; exit 1; }
  kill -0 "$UP_PID" 2>/dev/null || { echo "queue_shard: the uploader is not running; stopping the merge" >&2; exit 1; }
  n=$(find "$q" -maxdepth 1 -name '*.bin' | wc -l)
  [ "$n" -lt "${UPQ_MAX:-8}" ] && break
  sleep 2
done
mv "$1" "$q/"
