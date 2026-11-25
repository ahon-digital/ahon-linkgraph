#!/usr/bin/env python3
"""Is the link history keeping up? Standard library only, no token (public data and public run list).

    watch.py [--stale-hours 8] [--failed-runs 2] [--kick-file PATH] [--kick-hours 3]

Prints one line. Exit 0: healthy. Exit 1: needs a person (the line says why), for a caller that pages:
    msg=$(python3 scripts/watch.py) || notify "$msg"
Needs a person when: the history is behind Common Crawl and nothing was added for --stale-hours; the last
--failed-runs runs of the build workflow all failed; or something it must read cannot be read. A watcher that goes
quiet when it breaks is the failure it exists to catch, so an unreadable source is a page, never a pass.

Self-heal (26 Nov): GitHub drops scheduled runs under load (two of the every-6-hours runs on 25 Nov never fired,
so release 52 waited 13 h). With --kick-file, when the history is behind Common Crawl, no build run is queued or
running, no run started in the last --kick-hours, and the last --failed-runs runs did not all fail, this writes the
kick file. The caller (which holds a GitHub token; this script never does) starts the build and deletes the file.
It never kicks on repeated failures: those need a person, and the page above says so.
"""
import argparse, datetime, json, os, subprocess, sys, urllib.request

DATASET = "https://huggingface.co/datasets/ahon-digital/linkgraph/resolve/main/releases.tsv"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
RUNS = "https://api.github.com/repos/ahon-digital/ahon-linkgraph/actions/workflows/build.yml/runs?per_page=100"
here = os.path.dirname(os.path.abspath(__file__))


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ahon-linkgraph-watch", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=float, default=8)
    ap.add_argument("--failed-runs", type=int, default=2)
    ap.add_argument("--kick-file", default="")
    ap.add_argument("--kick-hours", type=float, default=3)
    a = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        rows = [l.split("\t") for l in get(DATASET).splitlines() if l.strip()]
        done, last, added = len(rows), rows[-1][1], datetime.datetime.fromisoformat(rows[-1][4].replace("Z", "+00:00"))
    except Exception as e:
        return page(f"link history: cannot read the published release list ({type(e).__name__})")
    try:
        r = subprocess.run([sys.executable, os.path.join(here, "releases.py")], capture_output=True, text=True, timeout=120)
    except Exception as e:   # a timeout or a failed launch is a page, not a traceback
        return page(f"link history: cannot read Common Crawl's release list ({type(e).__name__})")
    if r.returncode:
        last = (r.stderr.strip().splitlines() or ["no output"])[-1]
        return page(f"link history: cannot read Common Crawl's release list ({last[:160]})")
    total = len(r.stdout.split())
    try:
        listed = json.loads(get(RUNS))["workflow_runs"]
        runs = [x for x in listed if x.get("status") == "completed" and x.get("conclusion") != "cancelled"]
    except Exception as e:
        return page(f"link history: cannot read the build runs ({type(e).__name__})")
    if len(runs) < a.failed_runs and len(listed) >= 100:   # the window holds too few finished runs to judge
        return page(f"link history: the last 100 build runs hold only {len(runs)} finished, non-cancelled ones")
    hours = (now - added).total_seconds() / 3600
    state = f"{done} of {total} releases, last {last} added {hours:.0f} h ago"
    recent = [x["conclusion"] for x in runs[:a.failed_runs]]
    failing = len(recent) == a.failed_runs and all(c == "failure" for c in recent)
    if a.kick_file and done < total and not failing and not any(x.get("status") in ACTIVE for x in listed):
        # the latest attempt's start, not the run's creation (Codex r2: a rerun of an old run is a recent start)
        starts = [datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
                  for x in listed for t in (x.get("run_started_at"), x.get("created_at")) if t]
        if not starts or (now - max(starts)).total_seconds() / 3600 >= a.kick_hours:
            with open(a.kick_file, "w") as f:
                f.write(f"{now.isoformat()} {done} of {total}\n")
    if failing:
        return page(f"link history: the last {a.failed_runs} build runs failed ({runs[0]['html_url']}); {state}")
    if done < total and hours > a.stale_hours:
        return page(f"link history: behind Common Crawl and nothing added for {hours:.0f} h; {state}")
    print(f"ok: {state}")
    return 0


def page(msg):
    print(" ".join(str(msg).split()))   # one line, whatever the source put in it
    return 1


if __name__ == "__main__":
    sys.exit(main())
