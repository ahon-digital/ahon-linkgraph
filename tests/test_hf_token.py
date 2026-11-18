#!/usr/bin/env python3
"""hf.py, no network: the token reaches huggingface.co only (first request and every redirect hop), and a download
is accepted only when its size and sha256 match what the repo records.

    python3 tests/test_hf_token.py        or        pytest tests/test_hf_token.py
"""
import hashlib, importlib.util, io, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    os.environ.update(HF_TOKEN="hf_testvalue", LG_REPO="org/name")
    spec = importlib.util.spec_from_file_location("hf", os.path.join(HERE, "..", "scripts", "hf.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Reply:
    def __init__(self, body=b"{}", status=200):
        self.status, self._b = status, io.BytesIO(body)
    def read(self, n=-1): return self._b.read(n)
    def close(self): pass


def test_first_request_scoping():
    hf = load(); seen = []
    hf.OPENER.open = lambda r, timeout=0: (seen.append((r.full_url, r.get_header("Authorization"))), Reply())[1]
    cases = {"https://huggingface.co/api/x": True, "https://s3.us-east-1.amazonaws.com/b/x": False,
             "https://huggingface.co.evil.example/x": False, "http://cas-bridge.xethub.hf.co/x": False}
    for url in cases:
        hf.req("POST", url)
    assert [bool(a) for _, a in seen] == list(cases.values())


def test_redirect_drops_token_off_huggingface():
    hf = load(); h = hf._Redirect()
    r = urllib.request.Request("https://huggingface.co/datasets/org/name/resolve/main/x", headers={"Authorization": "Bearer t"})
    off = h.redirect_request(r, None, 302, "Found", {}, "https://storage.example/object")
    same = h.redirect_request(r, None, 302, "Found", {}, "https://huggingface.co/other")
    assert off is not None and not off.has_header("Authorization") and "Authorization" not in off.unredirected_hdrs
    assert same is not None and same.get_header("Authorization") == "Bearer t"


def test_download_must_match_size_and_hash():
    hf = load(); body = b"0123456789abcdef"
    oid = hashlib.sha256(body).hexdigest()
    hf.meta = lambda remote: (len(body), oid)
    for served, ok in ((body, True), (body[:9], False), (body[:-1] + b"X", False)):
        hf.req = lambda method, url, stream=False, timeout=0, s=served: (200, Reply(s))
        got = []
        try:
            hf.fetch("history/h-00000.bin", got.append)
            accepted = True
        except SystemExit:
            accepted = False
        assert accepted == ok, (served, accepted)


def test_download_without_hash_refused():
    hf = load(); body = b"good"
    hf.meta = lambda remote: (len(body), None)
    hf.req = lambda method, url, stream=False, timeout=0: (200, Reply(b"evil"))
    try:
        hf.fetch("domains.txt", lambda c: None)
        accepted = True
    except SystemExit:
        accepted = False
    assert not accepted


def test_malformed_token_never_printed():
    import subprocess
    secret = "hf_abc\ndefsecretpart"
    r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "hf.py"), "squash"],
                       env=dict(os.environ, HF_TOKEN=secret, LG_REPO="org/name"), capture_output=True, text=True)
    assert r.returncode != 0 and "defsecretpart" not in r.stdout + r.stderr
    assert "not a well-formed" in r.stderr   # refused up front, before any request is built


def test_exception_text_scrubbed():
    hf = load()
    def boom(r, timeout=0):
        raise ValueError("Invalid header value b'Bearer hf_leakedvalue\\n'")
    hf.OPENER.open = boom
    try:
        hf.req("GET", "https://huggingface.co/api/x")
        msg = ""
    except SystemExit as e:
        msg = str(e)
    assert msg and "leakedvalue" not in msg


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            bad += 1
            print("FAIL", t.__name__, e)
    print(f"{len(tests)} checks, {bad} failed")
    sys.exit(1 if bad else 0)
