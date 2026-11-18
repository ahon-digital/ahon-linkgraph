#!/usr/bin/env python3
"""lg against a plain-Python reference. Standard library only.

    python3 tests/test_lg.py path/to/lg        exit 0 = every check passed

Builds four synthetic releases whose domains overlap and drift (names come and go, links appear, vanish and return),
runs the real pipeline (names -> dictmerge -> inbound -> merge) release by release with tiny shards, and compares
every link's first and last release with the reference. Then checks that bad input is refused.
"""
import os, random, shutil, subprocess, sys, tempfile

LG = os.path.abspath(sys.argv[1])
random.seed(11)
W = tempfile.mkdtemp(prefix="lg-test-")
fails, checks = [], 0


def run(*args, ok=True, stdin=None):
    p = subprocess.run([LG, *args], capture_output=True, text=True, input=stdin)
    if ok and p.returncode:
        raise SystemExit(f"lg {args[0]} failed: {p.stderr.strip()}")
    return p


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        fails.append(f"{name} {detail}".strip())


tlds = ["com", "org", "net", "co.uk", "io", "de"]
universe = sorted({f"{random.choice(tlds)}.{''.join(random.choices('abcdefghij', k=random.randint(2, 7)))}".replace("co.uk.", "uk.co.") for _ in range(900)})
releases = []
for r in range(4):
    names = sorted(random.sample(universe, random.randint(350, 600)))
    n = len(names)
    edges = set()
    for _ in range(random.randint(2500, 4500)):
        a = random.randrange(n)
        b = int(random.paretovariate(1.1)) % n if random.random() < 0.5 else random.randrange(n)
        if a != b:
            edges.add((a, b))
    releases.append((names, sorted(edges)))

ref = {}   # (target name, source name) -> [first, last]
prev_dir, prev_global = None, None
for ri, (names, edges) in enumerate(releases):
    d = os.path.join(W, f"r{ri}"); os.makedirs(d)
    vtx = "".join(f"{i}\t{nm}\t1\n" for i, nm in enumerate(names))
    with open(f"{d}/edges.txt", "w") as f:
        f.write("".join(f"{a}\t{b}\n" for a, b in edges))
    run("names", f"{d}/names.txt", stdin=vtx)
    run("dictmerge", prev_global or "-", f"{d}/names.txt", f"{d}/global.txt", f"{d}/map_old.u32", f"{d}/map_rel.u32")
    run("inbound", str(len(names)), str(len(edges)), f"{d}/map_rel.u32", f"{d}/rel.bin", f"cat {d}/edges.txt")
    hd = f"{d}/hist"; os.makedirs(hd)
    prevcmd = f"cat {prev_dir}/h-*.bin" if prev_dir else "-"
    run("merge", str(ri), prevcmd, f"{d}/map_old.u32", f"{d}/rel.bin", hd, "2")
    for a, b in edges:
        k = (names[b], names[a])
        if k in ref:
            ref[k][1] = ri
        else:
            ref[k] = [ri, ri]
    prev_dir, prev_global = hd, f"{d}/global.txt"

shards = len([x for x in os.listdir(prev_dir) if x.startswith("h-")])
check("several shards", shards >= 3, f"({shards})")
gl = open(prev_global).read().split("\n")[:-1]
check("global names = union", gl == sorted(set(n for nm, _ in releases for n in nm)))
dump = run("dump", prev_dir, prev_global).stdout.splitlines()
got = {}
for line in dump:
    t, s, f1, l1 = line.split("\t")
    got[(t, s)] = [int(f1), int(l1)]
check("every link with first and last", got == ref, f"(got {len(got)}, want {len(ref)}, diff {len(set((k, tuple(v)) for k, v in got.items()) ^ set((k, tuple(v)) for k, v in ref.items()))})")
check("dump rows unique", len(dump) == len(got))

# lookup reference reader on a few domains, including one never linked to and one unknown
for nm in random.sample(gl, 25) + ["zz.never-seen"]:
    dom = ".".join(reversed(nm.split(".")))
    out = run("lookup", prev_dir, prev_global, dom).stdout.splitlines()
    want = sorted((s, f1, l1) for (t, s), (f1, l1) in ref.items() if t == nm)
    rows = sorted((x.split("\t")[0], int(x.split("\t")[1]), int(x.split("\t")[2])) for x in out[1:])
    check("lookup " + dom, rows == want, f"({len(rows)} vs {len(want)})")

# on-shard hook: each closed shard is handed off (moved away) and the result is unchanged
d = os.path.join(W, "r3"); hook = f"{W}/hook.sh"; os.makedirs(f"{W}/moved/hist")
open(hook, "w").write(f"#!/bin/sh\nmv \"$1\" {W}/moved/hist/ && echo \"$1\" >> {W}/hooked.txt\n"); os.chmod(hook, 0o755)
os.makedirs(f"{W}/hooked")
run("merge", "3", f"cat {W}/r2/hist/h-*.bin", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/hooked", "2", hook)
shutil.copy(f"{W}/hooked/history.idx", f"{W}/moved/hist/history.idx")
left = [x for x in os.listdir(f"{W}/hooked") if x.startswith("h-")]
moved = run("dump", f"{W}/moved/hist", prev_global).stdout.splitlines()
check("on-shard hook leaves no shard on disk", not left, str(left[:3]))
check("on-shard hook result identical", moved == dump)
check("hook ran once per shard", len(open(f"{W}/hooked.txt").read().split()) == shards)
bad = f"{W}/badhook.sh"; open(bad, "w").write("#!/bin/sh\nexit 1\n"); os.chmod(bad, 0o755); os.makedirs(f"{W}/badhook")
r = run("merge", "3", f"cat {W}/r2/hist/h-*.bin", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/badhook", "2", bad, ok=False)
check("failing hook stops the merge", r.returncode != 0)

# refusals
r = run("names", f"{W}/bad.txt", ok=False, stdin="0\tcom.b\t1\n1\tcom.a\t1\n")
check("unsorted names refused", r.returncode != 0)
r = run("names", f"{W}/bad.txt", ok=False, stdin="0\tcom.a\t1\n2\tcom.b\t1\n")
check("skipped id refused", r.returncode != 0)
d = os.path.join(W, "r3")
names, edges = releases[3]
r = run("inbound", str(len(names)), str(len(edges) + 1), f"{d}/map_rel.u32", f"{W}/x.bin", f"cat {d}/edges.txt", ok=False)
check("wrong edge count refused", r.returncode != 0)
flip = f"{W}/flip.sh"
open(flip, "w").write(f"#!/bin/sh\nif [ -e {W}/once ]; then awk -F'\\t' 'NR==1{{$2=($2==0?1:0)}} {{print $1\"\\t\"$2}}' {d}/edges.txt; else touch {W}/once; cat {d}/edges.txt; fi\n")
os.chmod(flip, 0o755)
r = run("inbound", str(len(names)), str(len(edges)), f"{d}/map_rel.u32", f"{W}/x.bin", flip, ok=False)
check("second pass differing refused", r.returncode != 0, r.stderr.strip()[:80])
os.makedirs(f"{W}/again")
r = run("merge", "3", f"cat {prev_dir}/h-*.bin", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/again", "2", ok=False)
check("same release twice refused", r.returncode != 0)
unsorted_edges = f"{W}/unsorted.txt"
open(unsorted_edges, "w").write("".join(f"{a}\t{b}\n" for a, b in reversed(edges)))
r = run("inbound", str(len(names)), str(len(edges)), f"{d}/map_rel.u32", f"{W}/x.bin", f"cat {unsorted_edges}", ok=False)
check("edges not sorted by source refused", r.returncode != 0)

print(f"{checks} checks, {len(fails)} failed, {len(ref)} links, {len(gl)} domains, {shards} shards")
for f in fails:
    print("FAIL", f)
shutil.rmtree(W)
sys.exit(1 if fails else 0)
