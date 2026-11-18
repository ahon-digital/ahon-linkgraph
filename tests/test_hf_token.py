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


def drain(r):
    """Read a request body the way a real transport would (a streamed body is read to its end)."""
    if hasattr(r.data, "read"):
        while r.data.read(8192):
            pass


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
    hf.OPENER.open = lambda r, timeout=0: (drain(r), sent.append((r.full_url, dict(r.header_items()))), Ok())[2]
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
    hf.OPENER.open = lambda r, timeout=0: (drain(r), sent.append(dict(r.header_items())), Ok())[2]
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False); f.write(b"data"); f.close()
    hf.put(f.name, "x.bin", f.name + ".manifest")
    assert sent and all(hf.TOKEN not in str(h) for h in sent), sent
    r = urllib.request.Request("https://huggingface.co/x", headers={"X-Echo-Token": hf.TOKEN, "Accept": "a"})
    off = hf._Redirect().redirect_request(r, None, 302, "Found", {}, "https://storage.example/o")
    assert off is not None and hf.TOKEN not in str(off.header_items()) and off.has_header("Accept")


def test_boundary_refuses_token_anywhere_off_host():
    hf = load(); b = hf._Boundary(); T = hf.TOKEN
    bad = [urllib.request.Request("https://storage.example/o?echo=" + T),
           urllib.request.Request("https://storage.example/o", headers={"X" + T: "1"}),
           urllib.request.Request("https://storage.example/o", headers={"X-A": "v" + T}),
           urllib.request.Request("https://storage.example/o", data=b"body " + T.encode())]
    for r in bad:
        try:
            b._check(r); refused = False
        except SystemExit:
            refused = True
        assert refused, r.full_url
    b._check(urllib.request.Request("https://huggingface.co/x", headers={"Authorization": "Bearer " + T}))
    b._check(urllib.request.Request("https://storage.example/o", headers={"X-A": "clean"}))


def test_redirect_chain_stopped_before_off_host_hop():
    hf = load(); T = hf.TOKEN; sent = []
    import email.message, urllib.response
    class Fake(urllib.request.BaseHandler):
        handler_order = 100   # before urllib's real HTTPS handler: nothing in this test touches the network
        def https_open(self, req):
            sent.append(req.full_url)
            hdrs = email.message.Message()
            if hf._hf(req.full_url):
                hdrs["Location"] = "https://storage.example/object?echo=" + T
                r = urllib.response.addinfourl(io.BytesIO(b""), hdrs, req.full_url, 302); r.msg = "Found"
                return r
            r = urllib.response.addinfourl(io.BytesIO(b"ok"), hdrs, req.full_url, 200); r.msg = "OK"
            return r
    opener = urllib.request.build_opener(hf._Redirect, hf._Boundary, Fake)
    hf.OPENER = opener
    try:
        hf.req("GET", "https://huggingface.co/datasets/org/name/resolve/main/x", tries=1); refused = False
    except SystemExit:
        refused = True
    assert refused and sent == ["https://huggingface.co/datasets/org/name/resolve/main/x"], sent   # first hop sent, off-host hop never
    fresh = load()
    assert any(isinstance(h, fresh._Boundary) for h in fresh.OPENER.handlers)   # the real opener carries the boundary


def test_plain_print_is_redacted():
    code, out, tok = _guard_output(lambda hf: print("echo " + hf.TOKEN, file=sys.stderr))
    assert tok not in out and "[token]" in out


def test_upload_of_a_file_holding_the_token_refused():
    hf = load(); import tempfile
    f = tempfile.NamedTemporaryFile(delete=False)
    f.write(b"a" * ((1 << 22) - 5) + hf.TOKEN.encode() + b"tail"); f.close()   # the token straddles the 4 MB read
    try:
        with open(f.name, "rb") as fh:
            hf.sha256_size(fh)
        refused = False
    except SystemExit:
        refused = True
    assert refused


def test_unscanned_stream_refused_off_host():
    hf = load(); b = hf._Boundary(); import tempfile
    f = tempfile.TemporaryFile()
    r = urllib.request.Request("https://storage.example/o", data=f, method="PUT")
    try:
        b._check(r); refused = False
    except SystemExit:
        refused = True
    assert refused
    g = urllib.request.Request("https://storage.example/o", data=hf._StreamGuard(f), method="PUT")
    b._check(g)                                   # the guarded stream is the one allowed form


def test_token_split_across_writes_redacted():
    def split(hf):
        T = hf.TOKEN
        sys.stderr.write("x " + T[:5]); sys.stderr.write(T[5:] + " y\n")
    code, out, tok = _guard_output(split)
    assert tok not in out and "[token]" in out


def test_history_bytes_pass_unchanged():
    hf = load(); body = b"LGH1" + hf.TOKEN.encode() + b"rest"
    oid = hashlib.sha256(body).hexdigest()
    hf.meta = lambda remote: (len(body), oid)
    hf.req = lambda method, url, stream=False, timeout=0: (200, Reply(body))
    got = []
    hf.fetch("history/h-00000.bin", got.append)
    assert b"".join(got) == body


def test_only_https_ever_leaves():
    hf = load(); b = hf._Boundary()
    for url in ("ftp://user:" + hf.TOKEN + "@storage.example/o", "http://huggingface.co/x", "file:///etc/passwd"):
        try:
            b._check(urllib.request.Request(url)); refused = False
        except SystemExit:
            refused = True
        assert refused, url
    names = {type(h).__name__ for h in hf.OPENER.handlers}
    assert not names & {"FTPHandler", "FileHandler", "DataHandler", "HTTPHandler", "ProxyHandler"}, names


def test_file_overwritten_during_upload_refused():
    hf = load(); import json as _j, tempfile
    f = tempfile.NamedTemporaryFile(delete=False); f.write(b"clean-data-0123"); f.close()
    def negotiate(method, url, data=None, headers=None, stream=False, timeout=0, tries=4):
        with open(f.name, "r+b") as w:          # someone rewrites the file in place while we negotiate
            w.write(b"other-data-9876")
        return 200, _j.dumps({"objects": [{"actions": {"upload": {"href": "https://storage.example/o", "header": {}}}}]}).encode()
    hf.req = negotiate
    class Ok:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    hf.OPENER.open = lambda r, timeout=0: (drain(r), Ok())[1]
    try:
        hf.put(f.name, "x.bin", f.name + ".m"); refused = False
    except SystemExit:
        refused = True
    assert refused and not os.path.exists(f.name + ".m")


def _refused(fn):
    try:
        fn(); return False
    except SystemExit:
        return True


def test_malformed_listing_is_never_empty():
    for body in (b'[{"path": "releases.tsv", "type": "fi', b'{"not": "a list"}', b'[{"type": "file"}]', b'[1, 2]'):
        hf = load()
        hf.req = lambda method, url, data=None, headers=None, stream=False, timeout=0, tries=4, b=body: (200, b)
        assert _refused(lambda: hf.meta("releases.tsv")), body
    hf = load()
    hf.req = lambda method, url, data=None, headers=None, stream=False, timeout=0, tries=4: (404, b"")
    assert hf.meta("releases.tsv") is None                       # a real 404 is still a real absence


def test_fresh_needs_its_own_empty_answer():
    import json as _j
    def info(body, st=200):
        hf = load(); hf.req = lambda method, url, data=None, headers=None, stream=False, timeout=0, tries=4: (st, body); return hf
    assert info(b"", 404).fresh() == 0
    assert info(_j.dumps({"siblings": [{"rfilename": "README.md"}, {"rfilename": ".gitattributes"}]}).encode()).fresh() == 0
    assert info(_j.dumps({"siblings": [{"rfilename": "history/history.idx"}]}).encode()).fresh() == 3
    for bad in (b"{oops", b'{"siblings": "x"}', b'{"siblings": [{"name": 1}]}', b"[]"):
        assert _refused(lambda: info(bad).fresh()), bad


def test_multipart_parts_in_numeric_order():
    hf = load(); import json as _j, tempfile
    body = b"abcdefghijkl"; f = tempfile.NamedTemporaryFile(delete=False); f.write(body); f.close()
    hdr = {"chunk_size": "1"}; hdr.update({str(i): f"https://storage.example/p{i}" for i in range(1, 13)})
    got = {}
    def neg(method, url, data=None, headers=None, stream=False, timeout=0, tries=4):
        if url.endswith("/objects/batch"):
            return 200, _j.dumps({"objects": [{"actions": {"upload": {"href": "https://huggingface.co/complete", "header": hdr}}}]}).encode()
        got["complete"] = _j.loads(data); return 200, b"{}"
    hf.req = neg
    class Ok:
        status = 200; headers = {"ETag": "e"}
        def __init__(self, u): self.headers = {"ETag": u}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def send(r, timeout=0):
        got[int(r.full_url.rsplit("p", 1)[1])] = r.data; return Ok(r.full_url)
    hf.OPENER.open = send
    hf.put(f.name, "x.bin", f.name + ".m")
    assert b"".join(got[i] for i in range(1, 13)) == body
    assert [p["partNumber"] for p in got["complete"]["parts"]] == list(range(1, 13))
    assert [p["etag"] for p in got["complete"]["parts"]] == [f"https://storage.example/p{i}" for i in range(1, 13)]


def _guard_output(fn):
    hf = load()
    err = io.StringIO(); keep = sys.stdout, sys.stderr
    sys.stderr = err
    try:
        hf.guarded(lambda: fn(hf))
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        sys.stdout, sys.stderr = keep
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
