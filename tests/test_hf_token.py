#!/usr/bin/env python3
"""hf.py sends the token to huggingface.co only: never to storage addresses or look-alike hosts. No network."""
import importlib.util, os, sys, urllib.request

os.environ.update(HF_TOKEN="hf_test_value", LG_REPO="org/name")
spec = importlib.util.spec_from_file_location("hf", os.path.join(os.path.dirname(__file__), "..", "scripts", "hf.py"))
hf = importlib.util.module_from_spec(spec); spec.loader.exec_module(hf)
seen = []
class Reply:
    status = 200
    def read(self): return b"{}"
    def close(self): pass
urllib.request.urlopen = lambda r, timeout=0: (seen.append((r.full_url, r.get_header("Authorization"))), Reply())[1]
cases = {"https://huggingface.co/api/x": True, "https://s3.us-east-1.amazonaws.com/b/x": False,
         "https://huggingface.co.evil.example/x": False, "http://cas-bridge.xethub.hf.co/x": False}
for url in cases:
    hf.req("POST", url)
bad = [(u, a) for (u, a), want in zip(seen, cases.values()) if bool(a) != want]
print(f"{len(cases)} checks, {len(bad)} failed")
sys.exit(1 if bad else 0)
