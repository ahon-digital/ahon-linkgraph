#!/usr/bin/env python3
"""run.sh's state decision with a stub hf.py, no network. The listing says "empty" for all three pieces; a fresh
start must still need the second, independent answer, and must stop when that answer is "holds data" or unknown.

    python3 tests/test_run_state.py        or        pytest tests/test_run_state.py
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = '''#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
if a[:1] in (["get"], ["exists"]): sys.exit(3)          # the listing: every piece absent
if a[:1] == ["fresh"]: sys.exit(int(os.environ["STUB_FRESH"]))
if a[:1] == ["ensure"]: open(os.environ["STUB_LOG"], "a").write("ensure\\n"); sys.exit(0)
sys.exit(0)
'''


def run(fresh_rc):
    d = tempfile.mkdtemp(prefix="lg-state-")
    shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(d, "scripts"))
    open(os.path.join(d, "scripts", "hf.py"), "w").write(STUB)
    open(os.path.join(d, "list.txt"), "w").write("cc-main-2019-test\n")
    log = os.path.join(d, "calls.txt")
    env = dict(os.environ, HF_TOKEN="hf_testvalue", LG_REPO="org/name", LG_WORK=os.path.join(d, "w"),
               LG_RELEASE_LIST=os.path.join(d, "list.txt"), LG_CC_BASE="file://" + os.path.join(d, "none"),
               STUB_FRESH=str(fresh_rc), STUB_LOG=log)
    p = subprocess.run(["bash", os.path.join(d, "scripts", "run.sh")], env=env, capture_output=True, text=True)
    wrote = os.path.exists(log)
    shutil.rmtree(d, ignore_errors=True)
    return p.returncode, p.stdout + p.stderr, wrote


def test_fresh_start_needs_second_opinion():
    for rc in (3, 1):                       # repo holds data / cannot tell
        code, out, wrote = run(rc)
        assert code != 0 and "repo holds data" in out and not wrote, (rc, out[-300:])
    code, out, wrote = run(0)               # both answers say empty: it proceeds (and fails later on the missing crawl)
    assert "repo holds data" not in out and "next: cc-main-2019-test" in out, out[-300:]


if __name__ == "__main__":
    try:
        test_fresh_start_needs_second_opinion()
        print("1 checks, 0 failed")
    except AssertionError as e:
        print("FAIL test_fresh_start_needs_second_opinion", e)
        print("1 checks, 1 failed")
        sys.exit(1)
