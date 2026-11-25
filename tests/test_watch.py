#!/usr/bin/env python3
"""scripts/watch.py with its three sources mocked: pages when it should, one line always, healthy when healthy.

    python3 tests/test_watch.py        or        pytest tests/test_watch.py
"""
import contextlib, datetime, io, json, os, subprocess, sys, types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import watch  # noqa: E402

fails, ran = [], [0]
NOW = datetime.datetime.now(datetime.timezone.utc)


def tsv(n, hours_ago):
    t = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "".join(f"{i}\tcc-main-20{18 + i // 12}-jan\t1\t1\t{t}\t{'a' * 64}\t1\n" for i in range(n))


def runs(*conclusions, started_hours_ago=1, active=None):
    t = (NOW - datetime.timedelta(hours=started_hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"status": "completed", "conclusion": c, "html_url": f"u{i}", "created_at": t} for i, c in enumerate(conclusions)]
    if active:
        rows.insert(0, {"status": active, "conclusion": None, "html_url": "ua", "created_at": t})
    return json.dumps({"workflow_runs": rows})


def go(release_tsv, run_list, cc=None, cc_exc=None, args=()):
    def get(url):
        return release_tsv if url == watch.DATASET else run_list
    def fake_run(*a, **k):
        if cc_exc:
            raise cc_exc
        return cc
    watch.get, watch.subprocess = get, types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    out, argv = io.StringIO(), sys.argv
    sys.argv = ["watch.py", *args]
    try:
        with contextlib.redirect_stdout(out):
            rc = watch.main()
    finally:
        sys.argv = argv
    return rc, out.getvalue()


def check(name, cond, detail=""):
    ran[0] += 1
    if not cond:
        fails.append(f"FAIL {name}: {detail}")


def body():
    ok51 = types.SimpleNamespace(returncode=0, stdout="\n".join(f"r{i}" for i in range(51)), stderr="")
    rc, out = go(tsv(51, 1), runs("success", "success"), ok51)
    check("caught up and passing is healthy", rc == 0 and out.startswith("ok:"), out)
    rc, out = go(tsv(36, 91), runs(*(["cancelled"] * 9 + ["failure", "failure"])), ok51)
    check("review 23 Nov: nine cancellations then two failures pages", rc == 1 and "failed" in out, out)
    rc, out = go(tsv(36, 91), runs(*(["cancelled"] * 100)), ok51)
    check("a window of only cancellations pages", rc == 1 and "only 0" in out, out)
    rc, out = go(tsv(36, 9), runs("success", "failure"), ok51)
    check("behind and stale pages", rc == 1 and "nothing added" in out, out)
    rc, out = go(tsv(36, 2), runs("success", "failure"), ok51)
    check("behind but recent is healthy", rc == 0, out)
    bad = types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback (most recent call last):\n  File x\nURLError: down\n")
    rc, out = go(tsv(36, 2), runs("success"), bad)
    check("a failing release list pages on one line", rc == 1 and out.count("\n") == 1 and "URLError" in out, repr(out))
    rc, out = go(tsv(36, 2), runs("success"), cc_exc=subprocess.TimeoutExpired("x", 120))
    check("a timed-out release list pages", rc == 1 and "TimeoutExpired" in out and out.count("\n") == 1, repr(out))
    rc, out = go("not\ta\trelease\tlog", runs("success"), ok51)
    check("an unreadable release log pages", rc == 1 and out.count("\n") == 1, repr(out))


def kick_case(release_tsv, run_list, cc, **kw):
    import tempfile
    d = tempfile.mkdtemp(); k = os.path.join(d, "kick")
    rc, out = go(release_tsv, run_list, cc, args=("--kick-file", k, *kw.get("args", ())))
    return rc, out, os.path.exists(k)


def kick_body():
    ok51 = types.SimpleNamespace(returncode=0, stdout="\n".join(f"r{i}" for i in range(52)), stderr="")
    rc, out, k = kick_case(tsv(51, 14), runs("success", "success", started_hours_ago=13), ok51)
    check("25 Nov: behind, idle, last run 13 h ago kicks (and still pages as stale)", k and rc == 1 and "nothing added" in out, out)
    rc, out, k = kick_case(tsv(51, 2), runs("success", started_hours_ago=4), ok51)
    check("behind, idle, last start 4 h ago kicks without paging", k and rc == 0, out)
    rc, out, k = kick_case(tsv(51, 2), runs("success", started_hours_ago=1), ok51)
    check("a run started 1 h ago: no kick", not k, out)
    rc, out, k = kick_case(tsv(51, 14), runs("success", started_hours_ago=13, active="in_progress"), ok51)
    check("a run in progress: no kick", not k, out)
    rc, out, k = kick_case(tsv(51, 14), runs("success", started_hours_ago=13, active="queued"), ok51)
    check("a run queued: no kick", not k, out)
    rc, out, k = kick_case(tsv(51, 14), runs("failure", "failure", started_hours_ago=13), ok51)
    check("two failures: no kick, page for a person", not k and rc == 1 and "failed" in out, out)
    rc, out, k = kick_case(tsv(52, 20), runs("success", started_hours_ago=20), ok51)
    check("caught up: no kick", not k and rc == 0, out)
    rc, out = go(tsv(51, 2), runs("success", started_hours_ago=4), ok51)
    check("without --kick-file nothing changes", rc == 0, out)
    rerun = json.loads(runs("success", started_hours_ago=13))
    rerun["workflow_runs"][0]["run_started_at"] = (NOW - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rc, out, k = kick_case(tsv(51, 14), json.dumps(rerun), ok51)
    check("Codex r2: a rerun started 30 min ago blocks a kick", not k, out)


def test_watch():
    kick_body()
    body()
    assert not fails, "\n".join(fails)


if __name__ == "__main__":
    body()
    kick_body()
    print("\n".join(fails))
    print(f"{ran[0]} checks, {len(fails)} failed")
    sys.exit(1 if fails else 0)
