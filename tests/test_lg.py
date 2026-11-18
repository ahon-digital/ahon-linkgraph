#!/usr/bin/env python3
"""lg against a plain-Python reference. Standard library only.

    python3 tests/test_lg.py [path/to/lg]      exit 0 = every check passed (builds lg itself when no path is given)
    pytest tests/test_lg.py                     same, as one test

Builds four synthetic releases whose domains overlap and drift (names come and go, links appear, vanish and return),
runs the real pipeline (names -> dictmerge -> inbound -> merge) release by release with tiny shards, and compares
every link's first and last release with the reference. Then checks that bad input is refused.
"""
import os, random, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build():
    out = os.path.join(tempfile.mkdtemp(prefix="lg-bin-"), "lg")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", out, os.path.join(ROOT, "src", "lg.c")], check=True)
    return out


def main(lg):
    global LG, W, fails, checks
    LG = os.path.abspath(lg)
    random.seed(11)
    W = tempfile.mkdtemp(prefix="lg-test-")
    fails, checks = [], 0
    try:
        body()
    finally:
        shutil.rmtree(W, ignore_errors=True)
    print(f"{checks} checks, {len(fails)} failed")
    for f in fails:
        print("FAIL", f)
    return fails


def test_lg():
    assert main(build()) == []


def run(*args, ok=True, stdin=None):
    p = subprocess.run([LG, *args], capture_output=True, text=True, input=stdin)
    if ok and p.returncode:
        raise SystemExit(f"lg {args[0]} failed: {p.stderr.strip()}")
    if not ok and "usage:" in p.stderr:   # a refusal test must be refused for its reason, never for bad arguments
        fails.append(f"lg {args[0]} refused on usage, not on its input: test is wrong")
    return p


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        fails.append(f"{name} {detail}".strip())



def body():
    global LG, W

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
        if ri % 2 == 1:                       # list the names out of byte order, as Common Crawl's 2018 releases do
            names = list(names); random.shuffle(names)
            edges = sorted({(a, b) for a, b in edges})
        if ri == 2:                           # one name listed twice, as 2019-may-jun-jul lists no.hordaland
            names = list(names) + [names[5]]  # the extra id gets links of its own
            extra = len(names) - 1
            edges = sorted(set(edges) | {(extra, 1), (2, extra), (extra, 9)} - {(extra, extra)})
        vtx = "".join(f"{i}\t{nm}\t1\n" for i, nm in enumerate(names))
        with open(f"{d}/edges.txt", "w") as f:
            f.write("".join(f"{a}\t{b}\n" for a, b in edges))
        run("names", f"{d}/names.txt", f"{d}/order.u32", stdin=vtx)
        run("dictmerge", prev_global or "-", f"{d}/names.txt", f"{d}/global.txt", f"{d}/map_old.u32", f"{d}/map_rel.u32")
        run("inbound", str(len(names)), str(len(edges)), f"{d}/map_rel.u32", f"{d}/order.u32", f"{d}/rel.bin", f"cat {d}/edges.txt")
        hd = f"{d}/hist"; os.makedirs(hd)
        prevcmd = f"cat {prev_dir}/h-*.bin" if prev_dir else "-"
        previdx = f"{prev_dir}/history.idx" if prev_dir else "-"
        run("merge", str(ri), prevcmd, previdx, f"{d}/map_old.u32", f"{d}/rel.bin", hd, "2")
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
    run("merge", "3", f"cat {W}/r2/hist/h-*.bin", f"{W}/r2/hist/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/hooked", "2", hook)
    shutil.copy(f"{W}/hooked/history.idx", f"{W}/moved/hist/history.idx")
    left = [x for x in os.listdir(f"{W}/hooked") if x.startswith("h-")]
    moved = run("dump", f"{W}/moved/hist", prev_global).stdout.splitlines()
    check("on-shard hook leaves no shard on disk", not left, str(left[:3]))
    check("on-shard hook result identical", moved == dump)
    check("hook ran once per shard", len(open(f"{W}/hooked.txt").read().split()) == shards)
    bad = f"{W}/badhook.sh"; open(bad, "w").write("#!/bin/sh\nexit 1\n"); os.chmod(bad, 0o755); os.makedirs(f"{W}/badhook")
    r = run("merge", "3", f"cat {W}/r2/hist/h-*.bin", f"{W}/r2/hist/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/badhook", "2", bad, ok=False)
    check("failing hook stops the merge", r.returncode != 0)

    # refusals
    r = run("names", f"{W}/ok.txt", f"{W}/ok.u32", stdin="0\tcom.b\t1\n1\tcom.a\t1\n")
    check("names in any order accepted and sorted", open(f"{W}/ok.txt").read() == "com.a\ncom.b\n")
    r = run("names", f"{W}/dup.txt", f"{W}/dup.u32", stdin="0\tcom.b\t1\n1\tcom.a\t1\n2\tcom.b\t1\n")
    pos = [int.from_bytes(open(f"{W}/dup.u32", "rb").read()[i:i + 4], "little") for i in (0, 4, 8)]
    check("a name listed twice is one domain", open(f"{W}/dup.txt").read() == "com.a\ncom.b\n" and pos == [1, 0, 1], str(pos))
    many = "".join(f"{i}\tcom.same\t1\n" for i in range(1102))
    r = run("names", f"{W}/many.txt", f"{W}/many.u32", ok=False, stdin=many)
    check("a vertex file of duplicates refused", r.returncode != 0)
    r = run("names", f"{W}/c18.txt", f"{W}/c18.u32", stdin="0\tac.2-yr\t1\n1\tac.2\t1\n")   # the 2018 case
    check("2018 ordering (ac.2-yr before ac.2) accepted", open(f"{W}/c18.txt").read() == "ac.2\nac.2-yr\n")
    r = run("names", f"{W}/bad.txt", f"{W}/bad.u32", ok=False, stdin="0\tcom.a\t1\n2\tcom.b\t1\n")
    check("skipped id refused", r.returncode != 0)
    d = os.path.join(W, "r3")
    names, edges = releases[3]
    r = run("inbound", str(len(names)), str(len(edges) + 1), f"{d}/map_rel.u32", f"{d}/order.u32", f"{W}/x.bin", f"cat {d}/edges.txt", ok=False)
    check("wrong edge count refused", r.returncode != 0)
    flip = f"{W}/flip.sh"
    open(flip, "w").write(f"#!/bin/sh\nif [ -e {W}/once ]; then awk -F'\\t' 'NR==1{{$2=($2==0?1:0)}} {{print $1\"\\t\"$2}}' {d}/edges.txt; else touch {W}/once; cat {d}/edges.txt; fi\n")
    os.chmod(flip, 0o755)
    r = run("inbound", str(len(names)), str(len(edges)), f"{d}/map_rel.u32", f"{d}/order.u32", f"{W}/x.bin", flip, ok=False)
    check("second pass differing refused", r.returncode != 0, r.stderr.strip()[:80])
    os.makedirs(f"{W}/again")
    r = run("merge", "3", f"cat {prev_dir}/h-*.bin", f"{prev_dir}/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/again", "2", ok=False)
    check("same release twice refused", r.returncode != 0)
    # review findings, 18 Nov
    r2 = f"{W}/r2/hist"; n2 = len([x for x in os.listdir(r2) if x.startswith("h-")])
    for ri_bad in ("2", "4", "0"):
        os.makedirs(f"{W}/ri{ri_bad}")
        r = run("merge", ri_bad, f"cat {r2}/h-*.bin", f"{r2}/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/ri{ri_bad}", "2", ok=False)
        check(f"release index {ri_bad} after 2 refused", r.returncode != 0)
    os.makedirs(f"{W}/missing")
    last = sorted(x for x in os.listdir(r2) if x.startswith("h-"))[-1]
    r = run("merge", "3", f"ls {r2}/h-*.bin | grep -v {last} | xargs cat", f"{r2}/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/missing", "2", ok=False)
    check("missing last shard refused", r.returncode != 0, r.stderr.strip()[:80])
    os.makedirs(f"{W}/short")
    r = run("merge", "3", f"cat {r2}/h-*.bin | head -c $(( $(cat {r2}/h-*.bin | wc -c) - 7 ))", f"{r2}/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/short", "2", ok=False)
    check("cut-off history refused", r.returncode != 0, r.stderr.strip()[:80])
    q = f"{W}/hist'; true; : '"; os.makedirs(q)
    r = run("merge", "3", f"cat {r2}/h-*.bin", f"{r2}/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", q, "2", "false", ok=False)
    check("quote in path cannot hide a failing hook", r.returncode != 0)
    ident = f"{W}/ident17.u32"; open(ident, "wb").write(b"".join(i.to_bytes(4, "little") for i in range(17)))
    open(f"{W}/p1.txt", "w").write("4\t0\n16\t0\n"); open(f"{W}/p2.txt", "w").write("8\t0\n12\t0\n")
    alt = f"{W}/alt.sh"; open(alt, "w").write(f"#!/bin/sh\nif [ -e {W}/alt1 ]; then cat {W}/p2.txt; else touch {W}/alt1; cat {W}/p1.txt; fi\n"); os.chmod(alt, 0o755)
    r = run("inbound", "17", "2", ident, ident, f"{W}/c.bin", alt, ok=False)
    check("reviewer's fingerprint collision refused", r.returncode != 0, r.stderr.strip()[:80])
    open(f"{W}/p3.txt", "w").write("4\t1\n8\t5\n"); open(f"{W}/p4.txt", "w").write("8\t5\n4\t1\n")
    alt2 = f"{W}/alt2.sh"; open(alt2, "w").write(f"#!/bin/sh\nif [ -e {W}/alt2f ]; then cat {W}/p4.txt; else touch {W}/alt2f; cat {W}/p3.txt; fi\n"); os.chmod(alt2, 0o755)
    r = run("inbound", "17", "2", ident, ident, f"{W}/c.bin", alt2, ok=False)
    check("second pass out of order refused", r.returncode != 0)
    # review round 2 follow-ups
    open(f"{W}/p5.txt", "w").write("0\t1\n"); open(f"{W}/p6.txt", "w").write("18446744073709551616\t1\n")
    alt3 = f"{W}/alt3.sh"; open(alt3, "w").write(f"#!/bin/sh\nif [ -e {W}/alt3f ]; then cat {W}/p6.txt; else touch {W}/alt3f; cat {W}/p5.txt; fi\n"); os.chmod(alt3, 0o755)
    r = run("inbound", "17", "1", ident, ident, f"{W}/c.bin", alt3, ok=False)
    check("overflowing id in pass 2 refused", r.returncode != 0)
    import struct
    sh0 = sorted(x for x in os.listdir(r2) if x.startswith("h-"))[0]
    os.makedirs(f"{W}/forged/src"); [shutil.copy(f"{r2}/{x}", f"{W}/forged/src/{x}") for x in os.listdir(r2)]
    shl = sorted(x for x in os.listdir(r2) if x.startswith("h-"))[-1]
    data = open(f"{W}/forged/src/{shl}", "rb").read(); n = struct.unpack("<Q", data[4:12])[0]; pos = 12

    def varint(p):
        v = sh = 0
        while True:
            c = data[p]; p += 1; v |= (c & 127) << sh; sh += 7
            if c < 128: return v, p
    for _ in range(n - 1):                              # walk to the start of the last record
        _, pos = varint(pos); cnt, pos = varint(pos)
        for _ in range(cnt):
            _, pos = varint(pos); pos += 2
    forged = data[:4] + struct.pack("<Q", n - 1) + data[12:pos]   # drop the last record, lower the count to match
    open(f"{W}/forged/src/{shl}", "wb").write(forged)
    os.makedirs(f"{W}/forged/out")
    r = run("merge", "3", f"cat {W}/forged/src/h-*.bin", f"{W}/forged/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/forged/out", "2", ok=False)
    check("shard count rewritten without the index refused", r.returncode != 0, r.stderr.strip()[:80])
    # review round 3: a record shortened inside a shard (its link count lowered, its last link's bytes cut, the
    # shard's record count untouched) must be refused by the index's byte count
    os.makedirs(f"{W}/short2/src"); [shutil.copy(f"{r2}/{x}", f"{W}/short2/src/{x}") for x in os.listdir(r2)]
    data = open(f"{W}/short2/src/{shl}", "rb").read(); pos = 12; n = struct.unpack("<Q", data[4:12])[0]
    for _ in range(n - 1):
        _, pos = varint(pos); cnt, pos = varint(pos)
        for _ in range(cnt):
            _, pos = varint(pos); pos += 2
    _, p1 = varint(pos); cnt, p2 = varint(p1)
    if cnt >= 2 and cnt < 128:
        q = p2
        for _ in range(cnt - 1):
            _, q = varint(q); q += 2
        cut = data[:p1] + bytes([cnt - 1]) + data[p2:q]           # one link fewer, record count unchanged
        open(f"{W}/short2/src/{shl}", "wb").write(cut)
        os.makedirs(f"{W}/short2/out")
        r = run("merge", "3", f"cat {W}/short2/src/h-*.bin", f"{W}/short2/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/short2/out", "2", ok=False)
        check("record shortened inside a shard refused", r.returncode != 0, r.stderr.strip()[:80])
    else:
        check("fixture: last record has 2..127 links", False, str(cnt))
    # review round 4: numbers that wrap must be refused, as arguments and inside the index
    os.makedirs(f"{W}/wrap1")
    r = run("merge", "4294967299", f"cat {r2}/h-*.bin", f"{r2}/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/wrap1", "2", ok=False)
    check("wrapping release index refused", r.returncode != 0)
    os.makedirs(f"{W}/wrap2/src"); [shutil.copy(f"{r2}/{x}", f"{W}/wrap2/src/{x}") for x in os.listdir(r2)]
    lines = open(f"{W}/wrap2/src/history.idx").read().splitlines(); lines[0] = "#release\t4294967298"
    open(f"{W}/wrap2/src/history.idx", "w").write("\n".join(lines) + "\n"); os.makedirs(f"{W}/wrap2/out")
    r = run("merge", "3", f"cat {r2}/h-*.bin", f"{W}/wrap2/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/wrap2/out", "2", ok=False)
    check("wrapping release in the index refused", r.returncode != 0)
    # review round 5: the edge reader and every index field are strict
    for bad_edge in ("-1\t0\n", "1 \t0\n", "1\t0\t5\n", "1\t\n", "\t1\n", "1\n"):
        open(f"{W}/be.txt", "w").write(bad_edge)
        r = run("inbound", "17", "1", ident, ident, f"{W}/c.bin", f"cat {W}/be.txt", ok=False)
        check(f"malformed edge {bad_edge!r} refused", r.returncode != 0)
    os.makedirs(f"{W}/emptyf/src"); [shutil.copy(f"{r2}/{x}", f"{W}/emptyf/src/{x}") for x in os.listdir(r2)]
    lines = open(f"{W}/emptyf/src/history.idx").read().splitlines(); lines[1] = lines[1].replace("\t", "\t\t", 1)
    open(f"{W}/emptyf/src/history.idx", "w").write("\n".join(lines) + "\n"); os.makedirs(f"{W}/emptyf/out")
    r = run("merge", "3", f"cat {r2}/h-*.bin", f"{W}/emptyf/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/emptyf/out", "2", ok=False)
    check("empty index field refused", r.returncode != 0)
    open(f"{W}/ovf.bin", "wb").write(bytes([0xff] * 9 + [0x7f, 1, 1]))   # a 10-byte varint past 2^64
    os.makedirs(f"{W}/ovf")
    r = run("merge", "0", "-", "-", f"{d}/map_old.u32", f"{W}/ovf.bin", f"{W}/ovf", "2", ok=False)
    check("overflowing varint refused", r.returncode != 0)
    # review round 6: a carriage return is junk, never a line end, in the index and in vertex names
    os.makedirs(f"{W}/cr/src"); [shutil.copy(f"{r2}/{x}", f"{W}/cr/src/{x}") for x in os.listdir(r2)]
    lines = open(f"{W}/cr/src/history.idx").read().splitlines(); lines[1] = lines[1] + "\rJUNK"
    open(f"{W}/cr/src/history.idx", "w").write("\n".join(lines) + "\n"); os.makedirs(f"{W}/cr/out")
    r = run("merge", "3", f"cat {r2}/h-*.bin", f"{W}/cr/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/cr/out", "2", ok=False)
    check("carriage-return junk in the index refused", r.returncode != 0)
    r = run("names", f"{W}/crn.txt", f"{W}/crn.u32", ok=False, stdin="0\tcom.a\rjunk\t1\n")
    check("carriage return in a vertex name refused", r.returncode != 0)
    # review round 7: a NUL byte never ends a field early; checkidx validates a whole index
    r = run("names", f"{W}/nul.txt", f"{W}/nul.u32", ok=False, stdin="0\tcom.a\0junk\t1\n")
    check("NUL in a vertex name refused", r.returncode != 0)
    os.makedirs(f"{W}/nul/src"); [shutil.copy(f"{r2}/{x}", f"{W}/nul/src/{x}") for x in os.listdir(r2)]
    raw = open(f"{W}/nul/src/history.idx", "rb").read().split(b"\n"); raw[0] = raw[0] + b"\0junk"
    open(f"{W}/nul/src/history.idx", "wb").write(b"\n".join(raw)); os.makedirs(f"{W}/nul/out")
    r = run("merge", "3", f"cat {r2}/h-*.bin", f"{W}/nul/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/nul/out", "2", ok=False)
    check("NUL in the index refused", r.returncode != 0)
    good = run("checkidx", f"{r2}/history.idx").stdout.strip()
    check("checkidx names the release", good == "2", good)
    open(f"{W}/junkidx", "w").write(open(f"{r2}/history.idx").read() + "NOT AN INDEX\n")
    check("checkidx refuses a junk line", run("checkidx", f"{W}/junkidx", ok=False).returncode != 0)
    # review round 8: one rule for carriage returns in every reader; strict vertex lines; consistent index
    open(f"{W}/cr1.txt", "w").write("com.a\rjunk\n"); open(f"{W}/cr2.txt", "w").write("com.b\n")
    r = run("dictmerge", f"{W}/cr1.txt", f"{W}/cr2.txt", f"{W}/crout.txt", f"{W}/m1", f"{W}/m2", ok=False)
    check("carriage return in dictmerge input refused", r.returncode != 0)
    for bad_v in ("0\tcom.a\t1x\n", "0\tcom.a\t1\textra\n", "0\tcom.a\t\n", "0\tcom.a\t1\r\n"):
        r = run("names", f"{W}/bv.txt", f"{W}/bv.u32", ok=False, stdin=bad_v)
        check(f"vertex line {bad_v!r} refused", r.returncode != 0)
    good_v = run("names", f"{W}/gv.txt", f"{W}/gv.u32", ok=False, stdin="0\tcom.a\t1\n1\tcom.b\n")
    check("vertex lines with and without the host count accepted", good_v.returncode == 0, good_v.stderr.strip()[:60])
    lines = open(f"{r2}/history.idx").read().splitlines()
    bad = list(lines); i = next(k for k, l in enumerate(bad) if l and not l.startswith("#"))
    f0 = bad[i].split("\t"); bad[i] = "\t".join([f0[0], "999", f0[2]])        # first block of shard 0 not at byte 12
    open(f"{W}/badrel.idx", "w").write("\n".join(bad) + "\n")
    check("index block not at its shard's start refused", run("checkidx", f"{W}/badrel.idx", ok=False).returncode != 0)
    bad = list(lines); bad.insert(i + 1, "\t".join([f0[0], "13", "0"]))       # a target going backwards
    open(f"{W}/badrel2.idx", "w").write("\n".join(bad) + "\n")
    check("index first targets going backwards refused", run("checkidx", f"{W}/badrel2.idx", ok=False).returncode != 0)
    # review round 9: index descriptors must match the data; a link's first release is never after its last
    lines = open(f"{r2}/history.idx").read().splitlines()
    blk = [(k, l.split("\t")) for k, l in enumerate(lines) if l and not l.startswith("#")]
    pick = next((k, f) for (k, f), (k2, f2) in zip(blk, blk[1:]) if int(f2[2]) > int(f[2]) + 1)
    k, f = pick; bad = list(lines); bad[k] = "\t".join([f[0], f[1], str(int(f[2]) + 1)])
    os.makedirs(f"{W}/desc/src"); [shutil.copy(f"{r2}/{x}", f"{W}/desc/src/{x}") for x in os.listdir(r2) if x.startswith("h-")]
    open(f"{W}/desc/src/history.idx", "w").write("\n".join(bad) + "\n"); os.makedirs(f"{W}/desc/out")
    check("fixture: altered index still passes checkidx (only the data can tell)", run("checkidx", f"{W}/desc/src/history.idx", ok=False).returncode == 0)
    r = run("merge", "3", f"cat {W}/desc/src/h-*.bin", f"{W}/desc/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/desc/out", "2", ok=False)
    check("index block disagreeing with the data refused", r.returncode != 0, r.stderr.strip()[:80])
    os.makedirs(f"{W}/fl/src"); [shutil.copy(f"{r2}/{x}", f"{W}/fl/src/{x}") for x in os.listdir(r2)]
    lastsh = sorted(x for x in os.listdir(f"{W}/fl/src") if x.startswith("h-"))[-1]
    raw = bytearray(open(f"{W}/fl/src/{lastsh}", "rb").read()); raw[-2], raw[-1] = 2, 0     # last link: first 2, last 0
    open(f"{W}/fl/src/{lastsh}", "wb").write(raw); os.makedirs(f"{W}/fl/out")
    r = run("merge", "3", f"cat {W}/fl/src/h-*.bin", f"{W}/fl/src/history.idx", f"{d}/map_old.u32", f"{d}/rel.bin", f"{W}/fl/out", "2", ok=False)
    check("a link with first after last refused", r.returncode != 0, r.stderr.strip()[:80])
    # review round 10 follow-ups: a read error is never end of input; a shard always holds records
    os.makedirs(f"{W}/adir"); os.makedirs(f"{W}/rd")
    r = run("merge", "0", "-", "-", f"{d}/map_old.u32", f"{W}/adir", f"{W}/rd", "2", ok=False)
    check("unreadable release input refused", r.returncode != 0)
    r = run("dictmerge", f"{W}/adir", f"{W}/r0/names.txt", f"{W}/dm.txt", f"{W}/dm1", f"{W}/dm2", ok=False)
    check("unreadable old dictionary refused", r.returncode != 0)
    open(f"{W}/zero.idx", "w").write("#release\t0\n0\t12\t0\n#shard\t0\t0\t17\n")
    check("index shard with no records refused", run("checkidx", f"{W}/zero.idx", ok=False).returncode != 0)
    idx_lines = open(f"{r2}/history.idx").read().splitlines()
    check("index lists every shard's records", sum(1 for l in idx_lines if l.startswith("#shard")) == n2)
    unsorted_edges = f"{W}/unsorted.txt"
    open(unsorted_edges, "w").write("".join(f"{a}\t{b}\n" for a, b in reversed(edges)))
    r = run("inbound", str(len(names)), str(len(edges)), f"{d}/map_rel.u32", f"{d}/order.u32", f"{W}/x.bin", f"cat {unsorted_edges}", ok=False)
    check("edges not sorted by source refused", r.returncode != 0)

    print(f"{len(ref)} links, {len(gl)} domains, {shards} shards")


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1] if len(sys.argv) > 1 else build()) else 0)
