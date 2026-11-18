#!/usr/bin/env python3
"""Hugging Face dataset helper for the link graph build. Standard library only (no huggingface_hub).

Environment: HF_TOKEN (write token, org-scoped), LG_REPO (e.g. ahon-digital/linkgraph).

    hf.py ensure                       create the dataset (public: raw link history only, never scores) if missing
    hf.py get REMOTE OUT               download one file; exit 3 if it does not exist, 1 on any other failure
    hf.py exists REMOTE                exit 0 if the file exists, 3 if not, 1 if that cannot be told
    hf.py cat REMOTE...                stream files to stdout in order (previous history shards)
    hf.py put LOCAL REMOTE MANIFEST    upload LOCAL as a large-file object, append "REMOTE oid size" to MANIFEST
    hf.py commit MESSAGE MANIFEST [--delete-missing PREFIX]
                                       one commit pointing REMOTE paths at the uploaded objects; with
                                       --delete-missing, files under PREFIX not in the manifest are deleted
    hf.py squash                       squash the repo history so replaced objects stop counting as storage
Nothing here prints the token. Request bodies and logs carry paths, sizes and counts only.
"""
import hashlib, json, os, sys, time, urllib.error, urllib.parse, urllib.request

H = "https://huggingface.co"
TOKEN = os.environ.get("HF_TOKEN", "").strip()
REPO = os.environ.get("LG_REPO", "")
if not TOKEN or not REPO:
    sys.exit("hf.py: HF_TOKEN and LG_REPO are required")
# A token with anything but token characters would end up inside an error message; refuse it without echoing it.
if not TOKEN.startswith("hf_") or not TOKEN[3:].isalnum():
    sys.exit("hf.py: HF_TOKEN is not a well-formed Hugging Face token (value not shown)")
ORG, NAME = REPO.split("/", 1)


def _hf(url):
    return (urllib.parse.urlsplit(url).hostname or "") == "huggingface.co"


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but the token never leaves huggingface.co: a hop to any other host drops it."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _hf(newurl):
            for k, v in list(new.header_items()):
                if k.lower() in _SECRET_HEADERS or (TOKEN and TOKEN in str(v)):
                    new.remove_header(k)
        return new


_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie"}


class _Boundary(urllib.request.BaseHandler):
    """The one place the credential boundary is enforced. Every request urllib sends, first hop or redirect, passes
    through here: if it is not going to huggingface.co and the token appears anywhere in it (URL, any header name or
    value, an in-memory body), the request is refused, never sent. Filters elsewhere only strip what we can predict;
    this refuses what we cannot."""
    handler_order = 1000   # after every other pre-processor, so it sees the request exactly as it will be sent

    def _check(self, req):
        if urllib.parse.urlsplit(req.full_url).scheme != "https":
            raise SystemExit("hf.py: refused a non-https request")
        if TOKEN and not _hf(req.full_url):
            parts = [req.full_url] + [f"{k}: {v}" for k, v in req.header_items()]
            if isinstance(req.data, (bytes, bytearray)):
                parts.append(req.data.decode("latin-1"))
            elif req.data is not None and not isinstance(req.data, _StreamGuard):
                # a streamed body we cannot read here goes off-host only through the guard that checks every chunk
                raise SystemExit("hf.py: refused an unguarded streamed body off huggingface.co")
            if any(TOKEN in p for p in parts):
                raise SystemExit("hf.py: refused a request off huggingface.co that would have carried the token")
        return req

    http_request = https_request = _check


class _StreamGuard:
    """The only streamed upload body hf.py sends. Reads from one already-open file, refuses a chunk that would
    complete the token before handing it over, and hashes what it hands over so the sender can prove the bytes
    sent are the bytes negotiated."""
    def __init__(self, f):
        self._f, self._t, self._tail, self.sha = f, TOKEN.encode(), b"", hashlib.sha256()
    def read(self, n=-1):
        c = self._f.read(n)
        if self._t and self._t in self._tail + c:
            raise SystemExit("hf.py: refusing to upload a file that contains the token")
        self._tail = (self._tail + c)[-(len(self._t) - 1):] if self._t else b""
        self.sha.update(c)
        return c


def _opener():
    """HTTPS only: no FTP, file, data or plain-http handlers exist in it, and the boundary refuses any other scheme."""
    o = urllib.request.OpenerDirector()
    for h in (urllib.request.UnknownHandler(), urllib.request.HTTPSHandler(), _Redirect(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(), _Boundary()):
        o.add_handler(h)
    return o


OPENER = _opener()


def safe_headers(url, extra):
    """Headers for a request to url. Credential headers from anyone else (for example an upload action in a
    server reply) are always dropped; our token is added only for huggingface.co."""
    # By value as well as by name: no header that contains the token goes anywhere but huggingface.co.
    h = {k: v for k, v in (extra or {}).items() if k.lower() not in _SECRET_HEADERS and not (TOKEN and TOKEN in str(v))}
    if _hf(url):
        h["Authorization"] = "Bearer " + TOKEN
    return h


def redact(text):
    return str(text).replace(TOKEN, "[token]") if TOKEN else str(text)


def req(method, url, data=None, headers=None, stream=False, timeout=300, tries=4):
    # The token goes to huggingface.co only, never to an address taken from a reply that points elsewhere.
    h = safe_headers(url, headers)
    for attempt in range(tries):
        try:
            r = OPENER.open(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout)
            if stream:
                return r.status, r
            body = r.read()
            r.close()
            return r.status, body
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            return e.code, e.read()[:400]
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            raise SystemExit(f"hf.py: network failure on {method} {url.split('?')[0]}: {type(e).__name__}")
        except Exception as e:   # never let an exception text that may quote headers reach the log
            raise SystemExit(f"hf.py: request failed on {method} {url.split('?')[0]}: {type(e).__name__}")


def jsonbody(b):
    try:
        return json.loads(b)
    except ValueError:
        return {}


def ensure():
    st, b = req("POST", H + "/api/repos/create", json.dumps({"type": "dataset", "name": NAME, "organization": ORG, "private": os.environ.get("LG_PRIVATE") == "1"}).encode(),
                {"Content-Type": "application/json"})
    if st in (200, 409):
        print(json.dumps({"repo": REPO, "created": st == 200}))
        return 0
    sys.exit(f"hf.py: create failed {st}")


def resolve_url(remote):
    return f"{H}/datasets/{REPO}/resolve/main/{remote}"


_META = {}


def meta(remote):
    """(size, sha256) the repo records for a file; None if the file does not exist. A download is only accepted when
    it matches both: a connection that closes early is an error, never a shorter file."""
    folder = remote.rsplit("/", 1)[0] if "/" in remote else ""
    if folder not in _META:
        st, b = req("GET", f"{H}/api/datasets/{REPO}/tree/main/{folder}")
        if st == 404:
            _META[folder] = {}
        elif st != 200:
            sys.exit(f"hf.py: listing {folder or '/'} failed {st}")
        else:
            _META[folder] = {x["path"]: (x.get("lfs") or {}).get("size", x.get("size")) for x in jsonbody(b) if x.get("type") == "file"}
            _META[folder + "#oid"] = {x["path"]: (x.get("lfs") or {}).get("oid") for x in jsonbody(b) if x.get("type") == "file"}
    if remote not in _META[folder]:
        return None
    return _META[folder][remote], _META[folder + "#oid"][remote]


def fetch(remote, sink):
    m = meta(remote)
    if m is None:
        return 404
    size, oid = m
    if not oid or size is None:
        sys.exit(f"hf.py: {remote} has no large-file hash on record; refusing to accept it unverified")
    st, r = req("GET", resolve_url(remote), stream=True, timeout=600)
    if st != 200:
        sys.exit(f"hf.py: get {remote} failed {st}")
    h, n = hashlib.sha256(), 0
    while True:
        c = r.read(1 << 22)
        if not c:
            break
        h.update(c)
        n += len(c)
        sink(c)
    r.close()
    if n != size or (oid and h.hexdigest() != oid):
        sys.exit(f"hf.py: {remote} arrived incomplete or changed ({n} of {size} bytes)")
    return 200


def get(remote, out):
    with open(out + ".part", "wb") as f:
        st = fetch(remote, f.write)
    if st == 404:
        os.remove(out + ".part")
        return 3
    n = os.path.getsize(out + ".part")
    os.replace(out + ".part", out)
    print(json.dumps({"got": remote, "bytes": n}), file=sys.stderr)
    return 0


def cat(remotes):
    out = sys.stdout.buffer
    for remote in remotes:
        if fetch(remote, out.write) != 200:
            sys.exit(f"hf.py: {remote} is missing")
    out.flush()
    return 0


def sha256_size(f):
    """Hash and size of an open file to upload, in one pass that also refuses a file containing the token (checked
    across chunk boundaries). Our uploads are built from public Common Crawl data and never should hold it. Leaves
    the file rewound."""
    h, n, t, tail = hashlib.sha256(), 0, TOKEN.encode(), b""
    f.seek(0)
    if True:
        while True:
            c = f.read(1 << 22)
            if not c:
                break
            if t and t in tail + c:
                raise SystemExit("hf.py: refusing to upload a file that contains the token")
            tail = (tail + c)[-(len(t) - 1):] if t else b""
            h.update(c)
            n += len(c)
    f.seek(0)
    return h.hexdigest(), n


def put(local, remote, manifest):
    with open(local, "rb") as fh:     # one handle for the scan, the hash and the upload; the path is never reopened
        return _put(fh, remote, manifest)


def _put(fh, remote, manifest):
    oid, size = sha256_size(fh)
    lfs = {"Accept": "application/vnd.git-lfs+json", "Content-Type": "application/vnd.git-lfs+json"}
    st, b = req("POST", f"{H}/datasets/{REPO}.git/info/lfs/objects/batch",
                json.dumps({"operation": "upload", "transfers": ["basic", "multipart"], "hash_algo": "sha256",
                            "objects": [{"oid": oid, "size": size}]}).encode(), lfs)
    if st != 200:
        sys.exit(f"hf.py: upload negotiation failed {st}")
    obj = jsonbody(b)["objects"][0]
    if obj.get("error"):
        sys.exit(f"hf.py: upload refused: {obj['error']}")
    act = obj.get("actions", {})
    if "upload" in act:
        up, hdr = act["upload"], act["upload"].get("header", {})
        if "chunk_size" in hdr:
            cs = int(hdr["chunk_size"])
            parts = sorted(k for k in hdr if k.isdigit())
            etags = []
            sent, t, tail = hashlib.sha256(), TOKEN.encode(), b""
            if True:
                f = fh
                for i, k in enumerate(parts):
                    f.seek(i * cs)
                    chunk = f.read(cs)
                    if t and t in tail + chunk:      # across part boundaries too
                        raise SystemExit("hf.py: refusing to upload a file that contains the token")
                    tail = (tail + chunk)[-(len(t) - 1):] if t else b""
                    sent.update(chunk)
                    for attempt in range(4):
                        try:
                            pr = urllib.request.Request(hdr[k], data=chunk, headers=safe_headers(hdr[k], {}), method="PUT")
                            with OPENER.open(pr, timeout=900) as x:
                                etags.append({"partNumber": i + 1, "etag": x.headers["ETag"]})
                            break
                        except (urllib.error.URLError, TimeoutError):
                            if attempt == 3:
                                raise SystemExit(f"hf.py: part {i + 1} of {remote} failed")
                            time.sleep(20 * (attempt + 1))
            if sent.hexdigest() != oid:
                sys.exit(f"hf.py: {remote} changed while it was being uploaded; refusing it")
            st, _ = req("POST", up["href"], json.dumps({"oid": oid, "parts": etags}).encode(), lfs)
            if st not in (200, 201):
                sys.exit(f"hf.py: multipart completion failed {st}")
        else:
            fh.seek(0)
            body = _StreamGuard(fh)
            headers = safe_headers(up["href"], hdr)
            headers["Content-Length"] = str(size)
            r = urllib.request.Request(up["href"], data=body, headers=headers, method="PUT")
            with OPENER.open(r, timeout=3600) as x:
                if x.status not in (200, 201):
                    sys.exit(f"hf.py: upload of {remote} failed {x.status}")
            if body.sha.hexdigest() != oid:
                sys.exit(f"hf.py: {remote} changed while it was being uploaded; refusing it")
        if "verify" in act:
            st, _ = req("POST", act["verify"]["href"], json.dumps({"oid": oid, "size": size}).encode(), lfs)
            if st != 200:
                sys.exit(f"hf.py: verify of {remote} failed {st}")
    with open(manifest, "a") as m:
        m.write(f"{remote}\t{oid}\t{size}\n")
    print(json.dumps({"uploaded": remote, "bytes": size, "already_there": "upload" not in act}), file=sys.stderr)
    return 0


def tree(prefix):
    out, url = [], f"{H}/api/datasets/{REPO}/tree/main/{prefix}?recursive=true"
    st, b = req("GET", url)
    if st == 404:
        return out
    if st != 200:
        sys.exit(f"hf.py: listing {prefix} failed {st}")
    return [x["path"] for x in jsonbody(b) if x.get("type") == "file"]


def commit(message, manifest, delete_prefix=None):
    rows = [l.rstrip("\n").split("\t") for l in open(manifest) if l.strip()]
    if not rows:
        sys.exit("hf.py: empty manifest, nothing to commit")
    keep = {r[0] for r in rows}
    ops = [{"key": "header", "value": {"summary": message}}]
    ops += [{"key": "lfsFile", "value": {"path": p, "algo": "sha256", "oid": o, "size": int(s)}} for p, o, s in rows]
    deleted = 0
    if delete_prefix:
        for p in tree(delete_prefix):
            if p not in keep:
                ops.append({"key": "deletedFile", "value": {"path": p}})
                deleted += 1
    body = ("\n".join(json.dumps(o) for o in ops) + "\n").encode()
    st, b = req("POST", f"{H}/api/datasets/{REPO}/commit/main", body, {"Content-Type": "application/x-ndjson"}, timeout=900)
    if st != 200:
        sys.exit(f"hf.py: commit failed {st}: {b[:200]!r}")
    print(json.dumps({"committed": len(rows), "deleted": deleted, "commit": jsonbody(b).get("commitOid", "")[:12]}))
    return 0


def squash():
    st, b = req("POST", f"{H}/api/datasets/{REPO}/super-squash/main", json.dumps({"message": "squash"}).encode(), {"Content-Type": "application/json"})
    if st != 200:
        sys.exit(f"hf.py: squash failed {st}")
    print(json.dumps({"squashed": True}))
    return 0


def main():
    a = sys.argv[1:]
    if a[:1] == ["ensure"]:
        sys.exit(ensure())
    if a[:1] == ["domsha"]:
        sys.exit(dom_sha())
    if a[:1] == ["exists"] and len(a) == 2:
        sys.exit(0 if meta(a[1]) is not None else 3)
    if a[:1] == ["get"] and len(a) == 3:
        sys.exit(get(a[1], a[2]))
    if a[:1] == ["cat"] and len(a) >= 2:
        sys.exit(cat(a[1:]))
    if a[:1] == ["put"] and len(a) == 4:
        sys.exit(put(a[1], a[2], a[3]))
    if a[:1] == ["commit"] and len(a) in (3, 5):
        sys.exit(commit(a[1], a[2], a[4] if len(a) == 5 and a[3] == "--delete-missing" else None))
    if a[:1] == ["squash"]:
        sys.exit(squash())
    sys.exit(__doc__)


def dom_sha():
    """The sha256 and size Hugging Face records for domains.txt (no download), tab-separated; empty when absent."""
    m = meta("domains.txt")
    print(f"{m[1]}\t{m[0]}" if m else "")
    return 0


class _Redacting:
    """A text stdout/stderr that never writes the token, whatever printed it. Holds text until a newline (or a
    flush), so a token split across several write() calls is still seen whole. The binary side (cat's history
    bytes, which are data for lg and not a log) is left untouched: changing verified bytes would corrupt them."""
    def __init__(self, inner):
        self._i, self._buf = inner, ""
        self.buffer = getattr(inner, "buffer", None)
    def write(self, s):
        self._buf += s
        if "\n" in self._buf:
            done, self._buf = self._buf.rsplit("\n", 1)
            self._i.write(redact(done + "\n"))
        return len(s)
    def flush(self):
        if self._buf:
            self._i.write(redact(self._buf)); self._buf = ""
        return self._i.flush()
    def __getattr__(self, k): return getattr(self._i, k)


def guarded(fn):
    """Outermost guard: whatever happens inside, nothing that leaves this process carries the token."""
    out, err = _Redacting(sys.stdout), _Redacting(sys.stderr)
    sys.stdout, sys.stderr = out, err
    try:
        fn()
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            raise
        print(redact(e.code), file=sys.stderr)
        sys.exit(1)
    except BaseException as e:   # an unexpected error prints its type only, never its text or traceback
        print(f"hf.py: failed: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)
    finally:
        out.flush(); err.flush()


if __name__ == "__main__":
    guarded(main)
