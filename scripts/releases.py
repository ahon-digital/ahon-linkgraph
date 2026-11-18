#!/usr/bin/env python3
"""Common Crawl domain-level web graph releases in date order, 2018 onward. Standard library only.

    releases.py            one release name a line, oldest first

Order comes from Common Crawl's own web-graphs page, which lists releases newest first; this reverses it. 2017
releases are left out (older folder layout; history starts in 2018). A page we cannot read, or one listing fewer
releases than a sane minimum, is an error, never an empty list: an empty list would read as "nothing to do".
"""
import re, sys, urllib.request

PAGE = "https://commoncrawl.org/web-graphs"
html = urllib.request.urlopen(urllib.request.Request(PAGE, headers={"User-Agent": "ahon-linkgraph"}), timeout=60).read().decode("utf-8", "replace")
seen, order = set(), []
for m in re.finditer(r"cc-main-(\d{4})(?:-\d{2})?-[a-z]{3}(?:-[a-z]{3})*", html):
    name = m.group(0)
    if name in seen or name.endswith("-hostgraph") or int(m.group(1)) < 2018:
        continue
    seen.add(name)
    order.append(name)
if len(order) < 30:
    sys.exit(f"releases.py: only {len(order)} releases found on {PAGE}; refusing to guess")
print("\n".join(reversed(order)))
