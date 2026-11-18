#!/usr/bin/env python3
"""End to end through scripts/run.sh against a throwaway PRIVATE Hugging Face dataset, with synthetic releases
served from local files (curl reads file:// URLs). Folds three releases one run at a time, checks a fourth run does
nothing, downloads the published history and compares every link with the reference, then deletes the dataset.

    HF_TOKEN=... python3 tests/e2e_hf.py ./lg ahon-digital/linkgraph-e2e
"""
import gzip, json, os, random, shutil, subprocess, sys, tempfile, urllib.error, urllib.request

LG, REPO = os.path.abspath(sys.argv[1]), sys.argv[2]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = tempfile.mkdtemp(prefix="lg-e2e-")
random.seed(5)
env = dict(os.environ, LG_REPO=REPO, LG_PRIVATE="1", LG_CC_BASE="file://" + T + "/cc", LG_RELEASE_LIST=T + "/list.txt", LG_SHARD_KB="1")
fails = []

universe = sorted({f"com.{''.join(random.choices('abcdefgh', k=random.randint(2, 6)))}" for _ in range(500)})
names_list = []
ref = {}
for ri in range(3):
    R = f"cc-main-20{19 + ri}-test"
    names = sorted(random.sample(universe, random.randint(200, 320)))
    edges = sorted({(random.randrange(len(names)), random.randrange(len(names))) for _ in range(2500)})
    edges = [(a, b) for a, b in edges if a != b]
    d = f"{T}/cc/{R}/domain"; os.makedirs(d)
    with gzip.open(f"{d}/{R}-domain-vertices.txt.gz", "wt") as f:
        f.write("".join(f"{i}\t{n}\t1\n" for i, n in enumerate(names)))
    with gzip.open(f"{d}/{R}-domain-edges.txt.gz", "wt") as f:
        f.write("".join(f"{a}\t{b}\n" for a, b in edges))
    open(f"{d}/{R}-domain.properties", "w").write(f"nodes={len(names)}\narcs={len(edges)}\n")
    names_list.append(R)
    for a, b in edges:
        k = (names[b], names[a])
        ref[k] = [ref[k][0], ri] if k in ref else [ri, ri]
open(T + "/list.txt", "w").write("\n".join(names_list) + "\n")


def step(i):
    w = f"{T}/work{i}"
    p = subprocess.run(["bash", f"{ROOT}/scripts/run.sh"], env=dict(env, LG_WORK=w), capture_output=True, text=True)
    if p.returncode:
        print(p.stdout[-1500:], p.stderr[-1500:])
        raise SystemExit(f"run {i} failed")
    return p.stdout, w


shutil.copy(LG, ROOT + "/lg") if os.path.abspath(LG) != os.path.abspath(ROOT + "/lg") else None
def exists():
    r = urllib.request.Request(f"https://huggingface.co/api/datasets/{REPO}", headers={"Authorization": "Bearer " + os.environ["HF_TOKEN"]})
    try:
        urllib.request.urlopen(r, timeout=60); return True
    except urllib.error.HTTPError as e:
        if e.code in (401, 404): return False
        raise


try:
    p = subprocess.run(["bash", f"{ROOT}/scripts/run.sh"], env=dict(env, LG_WORK=f"{T}/dry", LG_DRY="1"), capture_output=True, text=True)
    if p.returncode or "uploaded nothing" not in p.stdout:
        fails.append("dry run failed"); print(p.stdout[-800:], p.stderr[-800:])
    if exists():
        fails.append("dry run created the dataset")
    for i in range(3):
        out, _ = step(i)
        if f"next: {names_list[i]}" not in out:
            fails.append(f"run {i} did not pick {names_list[i]}")
    out, w = step(3)
    if "nothing to do" not in out or os.path.exists(w + "/did_work"):
        fails.append("fourth run was not a no-op")
    # download what was published and compare
    H = "https://huggingface.co"
    hdr = {"Authorization": "Bearer " + os.environ["HF_TOKEN"]}
    def get(path):   # through hf.py, so the token never follows a redirect off huggingface.co
        out = f"{T}/dl.bin"
        subprocess.run(["python3", f"{ROOT}/scripts/hf.py", "get", path, out], env=env, check=True, capture_output=True)
        return open(out, "rb").read()
    d = T + "/pub/history"; os.makedirs(d)
    idx = get("history/history.idx")
    open(d + "/history.idx", "wb").write(idx)
    for sh in sorted({int(l.split(b"\t")[0]) for l in idx.splitlines() if not l.startswith(b"#")}):
        open(f"{d}/h-{sh:05d}.bin", "wb").write(get(f"history/h-{sh:05d}.bin"))
    open(T + "/pub/domains.txt", "wb").write(get("domains.txt"))
    rel = get("releases.tsv").decode().splitlines()
    if [r.split("\t")[1] for r in rel] != names_list:
        fails.append(f"releases.tsv {rel}")
    listing = json.loads(urllib.request.urlopen(urllib.request.Request(f"{H}/api/datasets/{REPO}/tree/main/history", headers=hdr)).read())
    remote_shards = sorted(x["path"] for x in listing if x["path"].endswith(".bin"))
    local_shards = sorted("history/" + x for x in os.listdir(d) if x.endswith(".bin"))
    if remote_shards != local_shards:
        fails.append(f"stale shards left: {len(remote_shards)} remote vs {len(local_shards)} indexed")
    dump = subprocess.run([LG, "dump", d, T + "/pub/domains.txt"], capture_output=True, text=True, check=True).stdout.splitlines()
    got = {(l.split("\t")[0], l.split("\t")[1]): [int(l.split("\t")[2]), int(l.split("\t")[3])] for l in dump}
    if got != ref:
        fails.append(f"published history differs: {len(got)} vs {len(ref)} links")
    # a domain list that is not the one the last release recorded must stop the run (hash + count in the log)
    open(f"{T}/other_domains.txt", "w").write("com.aaaa\ncom.zzzz\n"); man0 = f"{T}/dom.manifest"
    subprocess.run(["python3", f"{ROOT}/scripts/hf.py", "put", f"{T}/other_domains.txt", "domains.txt", man0], env=env, check=True, capture_output=True)
    subprocess.run(["python3", f"{ROOT}/scripts/hf.py", "commit", "e2e: swap the domain list", man0], env=env, check=True, capture_output=True)
    # the history is current here (no next release): the check must still run before "nothing to do"
    p = subprocess.run(["bash", f"{ROOT}/scripts/run.sh"], env=dict(env, LG_WORK=f"{T}/swapdom"), capture_output=True, text=True)
    if p.returncode == 0 or "not the list the last release recorded" not in p.stdout:
        fails.append(f"swapped domain list not refused (exit {p.returncode}): {p.stdout[-200:]}")
    if get("history/history.idx") != idx:
        fails.append("history changed after the swapped-domains run")
    # review round 5: an EMPTY release log next to the history must stop the run too
    def commit_ops(summary, ops):
        body = (json.dumps({"key": "header", "value": {"summary": summary}}) + "\n" + "".join(json.dumps(o) + "\n" for o in ops)).encode()
        urllib.request.urlopen(urllib.request.Request(f"{H}/api/datasets/{REPO}/commit/main", data=body, method="POST",
                               headers=dict(hdr, **{"Content-Type": "application/x-ndjson"})), timeout=120)
    import base64
    open(f"{T}/empty.tsv", "wb").close(); man = f"{T}/empty.manifest"   # uploaded the way the builder uploads
    subprocess.run(["python3", f"{ROOT}/scripts/hf.py", "put", f"{T}/empty.tsv", "releases.tsv", man], env=env, check=True, capture_output=True)
    subprocess.run(["python3", f"{ROOT}/scripts/hf.py", "commit", "e2e: empty the release log", man], env=env, check=True, capture_output=True)
    p = subprocess.run(["bash", f"{ROOT}/scripts/run.sh"], env=dict(env, LG_WORK=f"{T}/emptylog"), capture_output=True, text=True)
    if p.returncode == 0 or "release log is empty" not in p.stdout:
        fails.append(f"empty release log not refused by its own rule (exit {p.returncode}): {p.stdout[-200:]}")
    if get("history/history.idx") != idx:
        fails.append("history changed after the empty-log run")
    # review round 4: remove only the release log; the next run must stop and the history must stay
    body = (json.dumps({"key": "header", "value": {"summary": "e2e: drop the release log"}}) + "\n" +
            json.dumps({"key": "deletedFile", "value": {"path": "releases.tsv"}}) + "\n").encode()
    urllib.request.urlopen(urllib.request.Request(f"{H}/api/datasets/{REPO}/commit/main", data=body, method="POST",
                           headers=dict(hdr, **{"Content-Type": "application/x-ndjson"})), timeout=120)
    p = subprocess.run(["bash", f"{ROOT}/scripts/run.sh"], env=dict(env, LG_WORK=f"{T}/nolog"), capture_output=True, text=True)
    if p.returncode == 0 or "dataset is partial" not in p.stdout:
        fails.append(f"missing release log not refused (exit {p.returncode})")
    idx_after = get("history/history.idx")
    if idx_after != idx:
        fails.append("history changed after the refused run")
    print(json.dumps({"releases": len(rel), "links": len(got), "shards": len(local_shards), "fails": fails}))
finally:
    if exists(): urllib.request.urlopen(urllib.request.Request(f"https://huggingface.co/api/repos/delete", method="DELETE",
        data=json.dumps({"type": "dataset", "name": REPO.split("/")[1], "organization": REPO.split("/")[0]}).encode(),
        headers={"Authorization": "Bearer " + os.environ["HF_TOKEN"], "Content-Type": "application/json"}), timeout=60)
    if os.path.exists(ROOT + "/lg") and os.path.abspath(LG) != os.path.abspath(ROOT + "/lg"):
        os.remove(ROOT + "/lg")
    shutil.rmtree(T)
sys.exit(1 if fails else 0)
