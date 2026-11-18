#!/usr/bin/env python3
"""End to end through scripts/run.sh against a throwaway PRIVATE Hugging Face dataset, with synthetic releases
served from local files (curl reads file:// URLs). Folds three releases one run at a time, checks a fourth run does
nothing, downloads the published history and compares every link with the reference, then deletes the dataset.

    HF_TOKEN=... python3 tests/e2e_hf.py ./lg ahon-digital/linkgraph-e2e
"""
import gzip, json, os, random, shutil, subprocess, sys, tempfile, urllib.request

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
try:
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
    print(json.dumps({"releases": len(rel), "links": len(got), "shards": len(local_shards), "fails": fails}))
finally:
    urllib.request.urlopen(urllib.request.Request(f"https://huggingface.co/api/repos/delete", method="DELETE",
        data=json.dumps({"type": "dataset", "name": REPO.split("/")[1], "organization": REPO.split("/")[0]}).encode(),
        headers={"Authorization": "Bearer " + os.environ["HF_TOKEN"], "Content-Type": "application/json"}), timeout=60)
    if os.path.exists(ROOT + "/lg") and os.path.abspath(LG) != os.path.abspath(ROOT + "/lg"):
        os.remove(ROOT + "/lg")
    shutil.rmtree(T)
sys.exit(1 if fails else 0)
