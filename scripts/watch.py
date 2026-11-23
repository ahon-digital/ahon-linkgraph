#!/usr/bin/env python3
"""Is the link history keeping up? Standard library only, no token (public data and public run list).

    watch.py [--stale-hours 8] [--failed-runs 2]

Prints one line. Exit 0: healthy. Exit 1: needs a person (the line says why), for a caller that pages:
    msg=$(python3 scripts/watch.py) || notify "$msg"
Needs a person when: the history is behind Common Crawl and nothing was added for --stale-hours; the last
--failed-runs runs of the build workflow all failed; or something it must read cannot be read. A watcher that goes
quiet when it breaks is the failure it exists to catch, so an unreadable source is a page, never a pass.
"""
import argparse, datetime, json, os, subprocess, sys, urllib.request

DATASET = "https://huggingface.co/datasets/ahon-digital/linkgraph/resolve/main/releases.tsv"
RUNS = "https://api.github.com/repos/ahon-digital/ahon-linkgraph/actions/workflows/build.yml/runs?per_page=10"
here = os.path.dirname(os.path.abspath(__file__))


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ahon-linkgraph-watch", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=float, default=8)
    ap.add_argument("--failed-runs", type=int, default=2)
    a = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        rows = [l.split("\t") for l in get(DATASET).splitlines() if l.strip()]
        done, last, added = len(rows), rows[-1][1], datetime.datetime.fromisoformat(rows[-1][4].replace("Z", "+00:00"))
    except Exception as e:
        return page(f"link history: cannot read the published release list ({type(e).__name__})")
    r = subprocess.run([sys.executable, os.path.join(here, "releases.py")], capture_output=True, text=True, timeout=120)
    if r.returncode:
        return page(f"link history: cannot read Common Crawl's release list ({r.stderr.strip()[:120]})")
    total = len(r.stdout.split())
    try:
        runs = [x for x in json.loads(get(RUNS))["workflow_runs"] if x.get("status") == "completed" and x.get("conclusion") != "cancelled"]
    except Exception as e:
        return page(f"link history: cannot read the build runs ({type(e).__name__})")
    hours = (now - added).total_seconds() / 3600
    state = f"{done} of {total} releases, last {last} added {hours:.0f} h ago"
    recent = [x["conclusion"] for x in runs[:a.failed_runs]]
    if len(recent) == a.failed_runs and all(c == "failure" for c in recent):
        return page(f"link history: the last {a.failed_runs} build runs failed ({runs[0]['html_url']}); {state}")
    if done < total and hours > a.stale_hours:
        return page(f"link history: behind Common Crawl and nothing added for {hours:.0f} h; {state}")
    print(f"ok: {state}")
    return 0


def page(msg):
    print(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
