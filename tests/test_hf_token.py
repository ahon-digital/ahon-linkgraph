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


def test_upload_reply_headers_cannot_carry_token_off_host():
    hf = load(); sent = []
    import json as _j
    def fake_req(method, url, data=None, headers=None, stream=False, timeout=0, tries=4):
        return 200, _j.dumps({"objects": [{"actions": {"upload": {"href": "https://storage.example/object",
                   "header": {"Authorization": "Bearer " + hf.TOKEN, "X-Amz": "1"}}}}]}).encode()
    hf.req = fake_req
    class Ok:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    hf.OPENER.open = lambda r, timeout=0: (sent.append((r.full_url, dict(r.header_items()))), Ok())[1]
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False); f.write(b"data"); f.close()
    hf.put(f.name, "x.bin", f.name + ".manifest")
    assert sent and all(hf.TOKEN not in str(h) for _, h in sent), sent


def test_token_in_any_reply_header_stays_on_huggingface():
    hf = load(); sent = []
    import json as _j
    hf.req = lambda method, url, data=None, headers=None, stream=False, timeout=0, tries=4: (200, _j.dumps({"objects": [{"actions": {
        "upload": {"href": "https://storage.example/object", "header": {"X-Echo-Token": "x" + hf.TOKEN + "y"}}}}]}).encode())
    class Ok:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    hf.OPENER.open = lambda r, timeout=0: (sent.append(dict(r.header_items())), Ok())[1]
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False); f.write(b"data"); f.close()
    hf.put(f.name, "x.bin", f.name + ".manifest")
    assert sent and all(hf.TOKEN not in str(h) for h in sent), sent
    r = urllib.request.Request("https://huggingface.co/x", headers={"X-Echo-Token": hf.TOKEN, "Accept": "a"})
    off = hf._Redirect().redirect_request(r, None, 302, "Found", {}, "https://storage.example/o")
    assert off is not None and hf.TOKEN not in str(off.header_items()) and off.has_header("Accept")


def _guard_output(fn):
    hf = load()
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            hf.guarded(lambda: fn(hf))
            code = 0
        except SystemExit as e:
            code = e.code
    return code, err.getvalue(), hf.TOKEN


def test_error_body_redacted():
    code, out, tok = _guard_output(lambda hf: sys.exit(f"hf.py: commit failed 400: Authorization: Bearer {hf.TOKEN}"))
    assert code == 1 and tok not in out and "[token]" in out


def test_exception_prints_type_only():
    def boom(hf):
        raise RuntimeError("header was Bearer " + hf.TOKEN)
    code, out, tok = _guard_output(boom)
    assert code == 1 and tok not in out and "RuntimeError" in out


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
