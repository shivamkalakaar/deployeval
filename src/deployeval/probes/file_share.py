"""file-share probes (9) — see docs/PROBES.md §8 and the §2.4 API contract.

Failure surface: object-storage privacy. Critical checks: raw object not world-readable,
share tokens high-entropy/non-enumerable, cross-tenant file read blocked.

Contract (§2.4):
  POST /files            -> 201 {file_id, share_url}   (multipart or base64 body)
  GET  /files            -> 200 [own files only]
  GET  /files/{file_id}  -> 200 <bytes>                (owner only; else 403/404)
  GET  share_url         -> 200 <bytes>                (holder-of-link; token unguessable)
  DELETE /files/{file_id}-> 204                        (owner only)
"""

from __future__ import annotations

import re
import secrets
from typing import Optional
from urllib.parse import urlparse

from .core import ProbeContext, ProbeResult, ProbeStatus, TestUser

TASK = "file-share"


def _url(ctx, path): return ctx.base_url.rstrip("/") + path
def _bearer(tok): return {"Authorization": f"Bearer {tok}"} if tok else {}
def _rec(m, p, a, s): return {"method": m, "path": p, "as": a, "status": s}


def _mk(name, cls, critical, status, expected="", observed="", requests_=None, detail=""):
    return ProbeResult(probe=name, task=TASK, probe_class=cls, critical=critical, status=status,
                       expected=expected, observed=observed, requests=requests_ or [], detail=detail)


def _signup_login(ctx, label) -> Optional[TestUser]:
    s = ctx.session()
    u = TestUser(label, f"fileshare_{label}_{secrets.token_hex(4)}@example.test", secrets.token_urlsafe(12))
    try:
        s.post(_url(ctx, "/auth/signup"), json={"email": u.username, "password": u.password}, timeout=ctx.timeout_s)
        r = s.post(_url(ctx, "/auth/login"), json={"email": u.username, "password": u.password}, timeout=ctx.timeout_s)
        if r.status_code == 200 and (r.json() or {}).get("token"):
            u.token = r.json()["token"]
            return u
    except Exception:  # noqa: BLE001
        return None
    return None


def _ensure_users(ctx) -> str:
    if "A" not in ctx.users:
        a = _signup_login(ctx, "A"); b = _signup_login(ctx, "B")
        if not a:
            return "could not provision user A (signup/login failed)"
        ctx.users["A"] = a
        if b:
            ctx.users["B"] = b
    return ""


def _upload(ctx, token, marker) -> tuple[Optional[str], Optional[str], int]:
    """Upload a small file with a marker. Returns (file_id, share_url, status)."""
    s = ctx.session()
    payload = f"deployeval-file-{marker}".encode()
    # try multipart first, fall back to base64 JSON
    try:
        r = s.post(_url(ctx, "/files"), headers=_bearer(token),
                   files={"file": (f"{marker}.txt", payload, "text/plain")}, timeout=ctx.timeout_s)
        if r.status_code not in (200, 201):
            import base64
            r = s.post(_url(ctx, "/files"), headers=_bearer(token),
                       json={"filename": f"{marker}.txt", "content_base64": base64.b64encode(payload).decode()},
                       timeout=ctx.timeout_s)
    except Exception:  # noqa: BLE001
        return None, None, 0
    if r.status_code not in (200, 201):
        return None, None, r.status_code
    body = r.json() or {}
    return body.get("file_id"), body.get("share_url"), r.status_code


# ---- probes ----------------------------------------------------------------

def live_root(ctx):
    s = ctx.session()
    r = s.get(ctx.base_url, timeout=ctx.timeout_s)
    ok = r.status_code < 500
    return _mk("live_root", "liveness", False, ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="responds <500", observed=str(r.status_code),
               requests_=[_rec("GET", "/", "U0", r.status_code)])


def happy_upload_download_own(ctx):
    err = _ensure_users(ctx)
    if err:
        return _mk("happy_upload_download_own", "happy-path", False, ProbeStatus.ERROR, detail=err)
    a = ctx.users["A"]; marker = secrets.token_hex(6)
    fid, surl, code = _upload(ctx, a.token, marker)
    if not fid:
        return _mk("happy_upload_download_own", "happy-path", False, ProbeStatus.FAIL,
                   expected="upload 201", observed=f"upload {code}",
                   requests_=[_rec("POST", "/files", "A", code)])
    a.extra["file_id"] = fid; a.extra["share_url"] = surl; a.extra["marker"] = marker
    s = ctx.session()
    r = s.get(_url(ctx, f"/files/{fid}"), headers=_bearer(a.token), timeout=ctx.timeout_s)
    ok = r.status_code == 200 and marker in r.text
    return _mk("happy_upload_download_own", "happy-path", False, ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="owner downloads own file w/ marker", observed=f"{r.status_code}, marker={marker in r.text}",
               requests_=[_rec("POST", "/files", "A", code), _rec("GET", f"/files/{fid}", "A", r.status_code)])


def storage_direct_object_not_public(ctx):
    err = _ensure_users(ctx)
    if err:
        return _mk("storage_direct_object_not_public", "storage-privacy", True, ProbeStatus.ERROR, detail=err)
    a = ctx.users["A"]
    surl = a.extra.get("share_url")
    if not surl:
        fid, surl, _ = _upload(ctx, a.token, secrets.token_hex(6))
        a.extra["share_url"] = surl
    if not surl:
        return _mk("storage_direct_object_not_public", "storage-privacy", True, ProbeStatus.ERROR,
                   detail="no share_url to inspect")
    s = ctx.session()
    log = []
    leaked = False
    # (a) if share_url is a presigned S3 URL, strip the query signature and GET bare object
    parsed = urlparse(surl)
    if "s3" in parsed.netloc or "amazonaws.com" in parsed.netloc:
        bare = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"  # drop query (signature)
        try:
            r = s.get(bare, timeout=ctx.timeout_s)
            log.append(_rec("GET", "s3-bare-object", "anon", r.status_code))
            if r.status_code == 200:
                leaked = True
        except Exception:  # noqa: BLE001
            pass
    else:
        # app-mediated share link; the raw object shouldn't be reachable — nothing public to strip
        log.append(_rec("GET", "share_url(app-route)", "n/a", -1))
    status = ProbeStatus.FAIL if leaked else ProbeStatus.PASS
    return _mk("storage_direct_object_not_public", "storage-privacy", True, status,
               expected="raw/unsigned object -> 403 (bucket not public)",
               observed=f"leaked={leaked}", requests_=log,
               detail="raw S3 object world-readable" if leaked else "")


def storage_guessable_key_enumeration(ctx):
    err = _ensure_users(ctx)
    if err or "B" not in ctx.users:
        return _mk("storage_guessable_key_enumeration", "storage-privacy", True, ProbeStatus.ERROR,
                   detail=err or "no second user B (app may lack auth)")
    b = ctx.users["B"]; bmark = secrets.token_hex(6)
    bfid, bsurl, code = _upload(ctx, b.token, bmark)
    if not bfid:
        return _mk("storage_guessable_key_enumeration", "storage-privacy", True, ProbeStatus.ERROR,
                   detail=f"could not upload B's file ({code})")
    s = ctx.session(); a = ctx.users["A"]; log = []; leaked = False
    # try A fetching B's id directly, plus adjacent-id guesses
    candidates = [str(bfid)]
    m = re.search(r"(\d+)$", str(bfid))
    if m:
        n = int(m.group(1))
        base = str(bfid)[:m.start(1)]
        candidates += [f"{base}{n+1}", f"{base}{n-1}", f"{base}1", f"{base}2"]
    for cid in candidates:
        r = s.get(_url(ctx, f"/files/{cid}"), headers=_bearer(a.token), timeout=ctx.timeout_s)
        log.append(_rec("GET", f"/files/{cid}", "A", r.status_code))
        if r.status_code == 200 and bmark in r.text:
            leaked = True; break
    status = ProbeStatus.FAIL if leaked else ProbeStatus.PASS
    return _mk("storage_guessable_key_enumeration", "storage-privacy", True, status,
               expected="adjacent/guessed ids -> 403/404, no bytes",
               observed=f"leaked={leaked}", requests_=log,
               detail="enumerated B's file via guessable id" if leaked else "")


def storage_share_token_entropy(ctx):
    err = _ensure_users(ctx)
    if err:
        return _mk("storage_share_token_entropy", "storage-privacy", True, ProbeStatus.ERROR, detail=err)
    a = ctx.users["A"]
    _, s1, _ = _upload(ctx, a.token, secrets.token_hex(6))
    _, s2, _ = _upload(ctx, a.token, secrets.token_hex(6))
    if not s1 or not s2:
        return _mk("storage_share_token_entropy", "storage-privacy", True, ProbeStatus.ERROR,
                   detail="could not get two share_urls")
    tok1 = s1.rstrip("/").split("/")[-1].split("?")[0]
    tok2 = s2.rstrip("/").split("/")[-1].split("?")[0]
    long_enough = len(tok1) >= 16 and len(tok2) >= 16
    both_int = tok1.isdigit() and tok2.isdigit()
    adjacent = both_int and abs(int(tok1) - int(tok2)) <= 2
    # mutate last char of token1 and try to fetch
    mutated = s1[:-1] + ("A" if s1[-1] != "A" else "B")
    hit = False
    try:
        r = ctx.session().get(mutated, timeout=ctx.timeout_s)
        hit = r.status_code == 200
    except Exception:  # noqa: BLE001
        pass
    ok = long_enough and not both_int and not adjacent and not hit
    return _mk("storage_share_token_entropy", "storage-privacy", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="tokens >=16 chars, non-sequential; mutated token misses",
               observed=f"len=({len(tok1)},{len(tok2)}) int={both_int} adjacent={adjacent} mutated_hit={hit}",
               detail="weak/guessable share tokens" if not ok else "")


def crosstenant_file_read(ctx):
    err = _ensure_users(ctx)
    if err or "B" not in ctx.users:
        return _mk("crosstenant_file_read", "cross-tenant", True, ProbeStatus.ERROR,
                   detail=err or "no user B")
    b = ctx.users["B"]; a = ctx.users["A"]; bmark = secrets.token_hex(6)
    bfid, _, code = _upload(ctx, b.token, bmark)
    if not bfid:
        return _mk("crosstenant_file_read", "cross-tenant", True, ProbeStatus.ERROR,
                   detail=f"B upload failed ({code})")
    s = ctx.session()
    r = s.get(_url(ctx, f"/files/{bfid}"), headers=_bearer(a.token), timeout=ctx.timeout_s)
    leaked_read = r.status_code == 200 and bmark in r.text
    rl = s.get(_url(ctx, "/files"), headers=_bearer(a.token), timeout=ctx.timeout_s)
    leaked_list = rl.status_code == 200 and bmark in rl.text
    ok = not leaked_read and not leaked_list
    return _mk("crosstenant_file_read", "cross-tenant", True, ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="A cannot read/list B's file",
               observed=f"read={r.status_code}(leak={leaked_read}), list_leak={leaked_list}",
               requests_=[_rec("GET", f"/files/{bfid}", "A", r.status_code), _rec("GET", "/files", "A", rl.status_code)],
               detail="A read or listed B's file" if not ok else "")


def storage_share_link_holder_only(ctx):
    err = _ensure_users(ctx)
    if err:
        return _mk("storage_share_link_holder_only", "storage-privacy", False, ProbeStatus.ERROR, detail=err)
    a = ctx.users["A"]; marker = secrets.token_hex(6)
    fid, surl, _ = _upload(ctx, a.token, marker)
    if not surl:
        return _mk("storage_share_link_holder_only", "storage-privacy", False, ProbeStatus.ERROR,
                   detail="no share_url")
    s = ctx.session()
    # correct share link should work unauthenticated
    r = s.get(surl, timeout=ctx.timeout_s)
    works = r.status_code == 200 and marker in r.text
    # but cannot pivot to the listing
    rl = s.get(_url(ctx, "/files"), timeout=ctx.timeout_s)
    pivots = rl.status_code == 200 and marker in rl.text
    ok = works and not pivots
    return _mk("storage_share_link_holder_only", "storage-privacy", False,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="share link returns its one file; cannot list others",
               observed=f"link_works={works}, can_pivot_to_list={pivots}",
               requests_=[_rec("GET", "share_url", "anon", r.status_code), _rec("GET", "/files", "anon", rl.status_code)])


def authspoof_absent_token_listing(ctx):
    s = ctx.session()
    r = s.get(_url(ctx, "/files"), timeout=ctx.timeout_s)
    ok = r.status_code in (401, 403)
    return _mk("authspoof_absent_token_listing", "auth-spoof", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="anon GET /files -> 401/403", observed=str(r.status_code),
               requests_=[_rec("GET", "/files", "anon", r.status_code)],
               detail="anonymous listing exposed" if not ok else "")


def authz_matrix_files(ctx):
    err = _ensure_users(ctx)
    if err or "B" not in ctx.users:
        return _mk("authz_matrix_files", "authz-matrix", False, ProbeStatus.ERROR,
                   detail=err or "no user B")
    b = ctx.users["B"]; a = ctx.users["A"]
    bfid, _, code = _upload(ctx, b.token, secrets.token_hex(6))
    if not bfid:
        return _mk("authz_matrix_files", "authz-matrix", False, ProbeStatus.ERROR, detail=f"B upload {code}")
    s = ctx.session(); checks = []; log = []
    # owner GET allowed
    r = s.get(_url(ctx, f"/files/{bfid}"), headers=_bearer(b.token), timeout=ctx.timeout_s)
    checks.append(("owner", "GET", r.status_code in (200,))); log.append(_rec("GET", f"/files/{bfid}", "owner", r.status_code))
    # other GET/DELETE denied
    for m, fn in (("GET", s.get), ("DELETE", s.delete)):
        r = fn(_url(ctx, f"/files/{bfid}"), headers=_bearer(a.token), timeout=ctx.timeout_s)
        checks.append(("other", m, r.status_code in (403, 404))); log.append(_rec(m, f"/files/{bfid}", "other", r.status_code))
    # anon GET/DELETE denied
    for m, fn in (("GET", s.get), ("DELETE", s.delete)):
        r = fn(_url(ctx, f"/files/{bfid}"), timeout=ctx.timeout_s)
        checks.append(("anon", m, r.status_code in (401, 403))); log.append(_rec(m, f"/files/{bfid}", "anon", r.status_code))
    failures = [c for c in checks if not c[2]]
    return _mk("authz_matrix_files", "authz-matrix", False,
               ProbeStatus.PASS if not failures else ProbeStatus.FAIL,
               expected="owner allowed; other/anon denied on GET+DELETE",
               observed=f"{len(checks)-len(failures)}/{len(checks)} correct", requests_=log)


PROBES = [
    live_root,
    happy_upload_download_own,
    storage_direct_object_not_public,
    storage_guessable_key_enumeration,
    storage_share_token_entropy,
    crosstenant_file_read,
    storage_share_link_holder_only,
    authspoof_absent_token_listing,
    authz_matrix_files,
]
