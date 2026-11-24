#!/usr/bin/env python3
"""Upload history shards while the merge is still writing the next ones. Standard library only.

    uploader.py QUEUE MANIFEST [WORKERS]

Watches QUEUE for *.bin files (the merge's shard hook moves each finished shard there), uploads each with the
reviewed `hf.py put` to history/<name> (WORKERS at a time, default 4), and deletes a file once it is uploaded.
Stops when QUEUE/DONE exists and nothing is left. Any failed upload writes QUEUE/FAILED and the exit is non-zero, so
the shard hook refuses the next shard, the merge stops and nothing is committed. Upload lines go to MANIFEST through
hf.py, exactly as before.
"""
import os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

here = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    queue, manifest = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) == 4 else 4
    if not 1 <= workers <= 16:
        sys.exit("uploader: WORKERS must be 1..16")
    failed = os.path.join(queue, "FAILED")
    taken, futures = set(), []

    def upload(name):
        path = os.path.join(queue, name)
        r = subprocess.run([sys.executable, os.path.join(here, "hf.py"), "put", path, f"history/{name}", manifest],
                           capture_output=True, text=True)
        if r.returncode:
            open(failed, "w").write(name + "\n")
            sys.stderr.write(f"uploader: {name} failed: {r.stderr.strip()[-300:]}\n")
            return False
        sys.stdout.write(r.stdout)
        sys.stdout.flush()
        os.remove(path)
        return True

    with ThreadPoolExecutor(workers) as ex:
        while True:
            if os.path.exists(failed):
                break
            done = os.path.exists(os.path.join(queue, "DONE"))
            for name in sorted(os.listdir(queue)):
                if name.endswith(".bin") and name not in taken:
                    taken.add(name)
                    futures.append(ex.submit(upload, name))
            if done and all(f.done() for f in futures) and not any(
                    n.endswith(".bin") and n not in taken for n in os.listdir(queue)):
                break
            time.sleep(1)
    ok = all(f.result() for f in futures) and not os.path.exists(failed)
    print(f'{{"uploaded_shards": {sum(1 for f in futures if f.result())}, "ok": {"true" if ok else "false"}}}')
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
