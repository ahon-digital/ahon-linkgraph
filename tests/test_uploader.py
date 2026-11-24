#!/usr/bin/env python3
"""scripts/uploader.py and scripts/queue_shard.sh with a stub hf.py: every shard uploads once and is removed, several
upload at once, a failure stops everything, the hook waits when the queue is full and refuses once the uploader failed
or is gone.

    python3 tests/test_uploader.py        or        pytest tests/test_uploader.py
"""
import os, shutil, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = """#!/usr/bin/env python3
import os, sys, time
a = sys.argv[1:]
assert a[0] == "put" and a[2] == "history/" + os.path.basename(a[1]), a
log = os.environ["STUB_LOG"]
open(log, "a").write("start " + a[2] + "\\n")
time.sleep(float(os.environ.get("STUB_SLEEP", "0.3")))
if os.path.basename(a[1]) == os.environ.get("STUB_FAIL", ""):
    sys.exit("stub: upload refused")
if os.path.basename(a[1]) == os.environ.get("STUB_VANISH", ""):
    os.remove(a[1])   # the uploader's own removal then raises: an exception inside a worker
open(a[3], "a").write(a[2] + "\\n")
open(log, "a").write("end " + a[2] + "\\n")
print('{"uploaded": "%s"}' % a[2])
"""
fails, ran = [], [0]


def check(name, cond, detail=""):
    ran[0] += 1
    if not cond:
        fails.append(f"FAIL {name}: {detail}")


def setup():
    d = tempfile.mkdtemp(prefix="lg-up-")
    os.makedirs(f"{d}/scripts"); os.makedirs(f"{d}/q"); os.makedirs(f"{d}/hist")
    for f in ("uploader.py", "queue_shard.sh"):
        shutil.copy(f"{ROOT}/scripts/{f}", f"{d}/scripts/{f}")
    open(f"{d}/scripts/hf.py", "w").write(STUB)
    open(f"{d}/manifest.tsv", "w").close()
    return d


def start(d, env, workers="4"):
    return subprocess.Popen([sys.executable, f"{d}/scripts/uploader.py", f"{d}/q", f"{d}/manifest.tsv", workers],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def hook(d, env, name, pid):
    p = f"{d}/hist/{name}"; open(p, "wb").write(b"x" * 10)
    e = dict(env, UPQ=f"{d}/q", UP_PID=str(pid))
    return subprocess.run(["sh", f"{d}/scripts/queue_shard.sh", p], env=e, capture_output=True, text=True, timeout=60)


def body():
    # 1. twelve shards through the hook: each uploaded once, removed, several at a time
    d = setup(); env = dict(os.environ, STUB_LOG=f"{d}/log")
    up = start(d, env)
    for i in range(12):
        r = hook(d, env, f"h-{i:05d}.bin", up.pid)
        check(f"hook accepts shard {i}", r.returncode == 0, r.stderr)
    open(f"{d}/q/DONE", "w").close()
    out, err = up.communicate(timeout=60)
    man = open(f"{d}/manifest.tsv").read().split()
    check("uploader exits 0 when all uploaded", up.returncode == 0, err)
    check("every shard uploaded exactly once", sorted(man) == [f"history/h-{i:05d}.bin" for i in range(12)], str(man))
    check("uploaded shards are removed", not [n for n in os.listdir(f"{d}/q") if n.endswith(".bin")], str(os.listdir(f"{d}/q")))
    depth, peak = 0, 0
    for line in open(f"{d}/log"):
        depth += 1 if line.startswith("start") else -1
        peak = max(peak, depth)
    check("several shards upload at once", peak >= 2, str(peak))
    shutil.rmtree(d)
    # 2. one failed upload: uploader exits non-zero, FAILED written, the hook then refuses
    d = setup(); env = dict(os.environ, STUB_LOG=f"{d}/log", STUB_FAIL="h-00002.bin", STUB_SLEEP="0.1")
    up = start(d, env)
    for i in range(3):
        hook(d, env, f"h-{i:05d}.bin", up.pid)
    t = time.time()
    while not os.path.exists(f"{d}/q/FAILED") and time.time() - t < 30:
        time.sleep(0.2)
    r = hook(d, env, "h-00003.bin", up.pid)
    check("hook refuses after a failed upload", r.returncode != 0 and "failed" in r.stderr, r.stderr)
    open(f"{d}/q/DONE", "w").close()
    up.communicate(timeout=60)
    check("uploader exits non-zero after a failed upload", up.returncode != 0)
    shutil.rmtree(d)
    # 3. a full queue makes the hook wait; it continues once the uploader drains it
    d = setup(); env = dict(os.environ, STUB_LOG=f"{d}/log", STUB_SLEEP="1.5", UPQ_MAX="2")
    up = start(d, env, "1")
    hook(d, env, "h-00000.bin", up.pid); hook(d, env, "h-00001.bin", up.pid)
    time.sleep(0.5)
    t = time.time(); r = hook(d, env, "h-00002.bin", up.pid); waited = time.time() - t
    check("hook waits while the queue is full", r.returncode == 0 and waited > 0.8, f"{waited:.2f}s {r.stderr}")
    open(f"{d}/q/DONE", "w").close(); up.communicate(timeout=60)
    shutil.rmtree(d)
    # 4. review r20: an exception inside a worker reaches the failure gate at once, with no DONE, so nothing waits
    d = setup(); env = dict(os.environ, STUB_LOG=f"{d}/log", STUB_VANISH="h-00001.bin", STUB_SLEEP="0.1")
    up = start(d, env)
    for i in range(3):
        hook(d, env, f"h-{i:05d}.bin", up.pid)
    try:
        up.communicate(timeout=20)   # DONE is never written: a wedged uploader would hang here
        exited = True
    except subprocess.TimeoutExpired:
        up.kill(); up.communicate(); exited = False
    check("a worker exception ends the uploader without waiting for the merge", exited and up.returncode != 0, str(up.returncode))
    check("a worker exception writes FAILED", os.path.exists(f"{d}/q/FAILED"))
    r = hook(d, env, "h-00009.bin", up.pid)
    check("the hook refuses after a worker exception", r.returncode != 0, r.stderr)
    shutil.rmtree(d)
    # 5. the uploader gone: the hook refuses instead of waiting forever
    d = setup(); env = dict(os.environ, STUB_LOG=f"{d}/log")
    dead = subprocess.Popen(["true"]); dead.wait()
    r = hook(d, env, "h-00000.bin", dead.pid)
    check("hook refuses when the uploader is gone", r.returncode != 0 and "not running" in r.stderr, r.stderr)
    shutil.rmtree(d)


def test_uploader():
    body()
    assert not fails, "\n".join(fails)


if __name__ == "__main__":
    body()
    print("\n".join(fails))
    print(f"{ran[0]} checks, {len(fails)} failed")
    sys.exit(1 if fails else 0)
