"""notes-auth probes (11) — see docs/PROBES.md §6 and the §2.1/§2.2 API contract.

Failure surface: auth + cross-tenant isolation. The headline adversarial check is
crosstenant_read_note_by_id (A reads B's note by id -> must be 403/404, NOT 200-with-data).

All probes assume the fixed contract:
  POST /auth/signup {email,password} -> 201 {user_id}
  POST /auth/login  {email,password} -> 200 {token}     (bearer token)
  POST /notes {title,body} -> 201 {id,...}              (owner = caller)
  GET/PUT/DELETE /notes/{id}                            (owner only; else 403/404)
Auth is `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import secrets
from typing import Optional

import requests

from .core import ProbeContext, ProbeResult, ProbeStatus, TestUser

TASK = "notes-auth"


# ---- helpers ---------------------------------------------------------------

def _url(ctx: ProbeContext, path: str) -> str:
    return ctx.base_url.rstrip("/") + path


def _bearer(token: Optional[str]) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _rec(method: str, path: str, as_: str, status) -> dict:
    return {"method": method, "path": path, "as": as_, "status": status}


def _mk(name: str, cls: str, critical: bool, status: ProbeStatus,
        expected="", observed="", requests_=None, detail="") -> ProbeResult:
    return ProbeResult(probe=name, task=TASK, probe_class=cls, critical=critical,
                       status=status, expected=expected, observed=observed,
                       requests=requests_ or [], detail=detail)


def _signup_login(ctx: ProbeContext, u: TestUser) -> tuple[Optional[str], list[dict], str]:
    """Provision one user through the app's own endpoints. Returns (token, request_log, error)."""
    s = ctx.session()
    log: list[dict] = []
    try:
        r = s.post(_url(ctx, "/auth/signup"), json={"email": u.username, "password": u.password},
                   timeout=ctx.timeout_s)
        log.append(_rec("POST", "/auth/signup", u.label, r.status_code))
        # 201 created or 409 already-exists are both acceptable for a re-run
        if r.status_code not in (200, 201, 409):
            return None, log, f"signup returned {r.status_code}"
        r = s.post(_url(ctx, "/auth/login"), json={"email": u.username, "password": u.password},
                   timeout=ctx.timeout_s)
        log.append(_rec("POST", "/auth/login", u.label, r.status_code))
        if r.status_code != 200:
            return None, log, f"login returned {r.status_code}"
        token = (r.json() or {}).get("token")
        if not token:
            return None, log, "login 200 but no token in body"
        return token, log, ""
    except Exception as exc:  # noqa: BLE001
        return None, log, f"provisioning error: {exc!r}"


def _ensure_users(ctx: ProbeContext) -> str:
    """Make sure ctx.users A and B exist and have tokens. Returns '' on success or an error string."""
    if "A" not in ctx.users:
        uniq = secrets.token_hex(4)
        ctx.users["A"] = TestUser("A", f"deployeval_a_{uniq}@example.test", secrets.token_urlsafe(12))
        ctx.users["B"] = TestUser("B", f"deployeval_b_{uniq}@example.test", secrets.token_urlsafe(12))
    for label in ("A", "B"):
        u = ctx.users[label]
        if not u.token:
            tok, _log, err = _signup_login(ctx, u)
            if err:
                return f"user {label}: {err}"
            u.token = tok
    return ""


def _create_note(ctx: ProbeContext, token: str, title="t", body="b") -> tuple[Optional[str], int]:
    s = ctx.session()
    r = s.post(_url(ctx, "/notes"), headers=_bearer(token),
               json={"title": title, "body": body}, timeout=ctx.timeout_s)
    if r.status_code not in (200, 201):
        return None, r.status_code
    return (r.json() or {}).get("id"), r.status_code


# ---- probes ----------------------------------------------------------------

def live_root(ctx: ProbeContext) -> ProbeResult:
    s = ctx.session()
    r = s.get(ctx.base_url, timeout=ctx.timeout_s)
    ok = r.status_code < 500
    return _mk("live_root", "liveness", False,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="service responds (<500)", observed=str(r.status_code),
               requests_=[_rec("GET", "/", "U0", r.status_code)])


def happy_signup_login(ctx: ProbeContext) -> ProbeResult:
    err = _ensure_users(ctx)
    if err:
        return _mk("happy_signup_login", "happy-path", False, ProbeStatus.FAIL,
                   expected="A and B can sign up and log in", observed=err, detail=err)
    return _mk("happy_signup_login", "happy-path", False, ProbeStatus.PASS,
               expected="A and B signed up + logged in", observed="both have tokens")


def happy_create_read_own_note(ctx: ProbeContext) -> ProbeResult:
    err = _ensure_users(ctx)
    if err:
        return _mk("happy_create_read_own_note", "happy-path", False, ProbeStatus.ERROR, detail=err)
    a = ctx.users["A"]
    nid, code = _create_note(ctx, a.token, "A note", "A body")
    if not nid:
        return _mk("happy_create_read_own_note", "happy-path", False, ProbeStatus.FAIL,
                   expected="A creates a note (201)", observed=f"create returned {code}",
                   requests_=[_rec("POST", "/notes", "A", code)])
    a.extra["note_id"] = nid
    s = ctx.session()
    r = s.get(_url(ctx, f"/notes/{nid}"), headers=_bearer(a.token), timeout=ctx.timeout_s)
    ok = r.status_code == 200
    return _mk("happy_create_read_own_note", "happy-path", False,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="A reads own note (200)", observed=str(r.status_code),
               requests_=[_rec("POST", "/notes", "A", code), _rec("GET", f"/notes/{nid}", "A", r.status_code)])


def crosstenant_read_note_by_id(ctx: ProbeContext) -> ProbeResult:
    """CRITICAL flagship: A must NOT be able to read B's note by id."""
    err = _ensure_users(ctx)
    if err:
        return _mk("crosstenant_read_note_by_id", "cross-tenant", True, ProbeStatus.ERROR, detail=err)
    b = ctx.users["B"]
    bnid, code = _create_note(ctx, b.token, "B secret", "B private body")
    if not bnid:
        return _mk("crosstenant_read_note_by_id", "cross-tenant", True, ProbeStatus.ERROR,
                   detail=f"could not create B's note (code {code})",
                   requests_=[_rec("POST", "/notes", "B", code)])
    b.extra["note_id"] = bnid
    s = ctx.session()
    r = s.get(_url(ctx, f"/notes/{bnid}"), headers=_bearer(ctx.users["A"].token), timeout=ctx.timeout_s)
    leaked = r.status_code == 200 and "B private body" in r.text
    status = ProbeStatus.FAIL if leaked else (
        ProbeStatus.PASS if r.status_code in (403, 404) else ProbeStatus.FAIL)
    return _mk("crosstenant_read_note_by_id", "cross-tenant", True, status,
               expected="A reading B's note -> 403/404, no content",
               observed=f"status {r.status_code}, leaked_body={leaked}",
               requests_=[_rec("POST", "/notes", "B", code), _rec("GET", f"/notes/{bnid}", "A", r.status_code)],
               detail="LEAK: A read B's note body" if leaked else "")


def crosstenant_update_note(ctx: ProbeContext) -> ProbeResult:
    err = _ensure_users(ctx)
    if err:
        return _mk("crosstenant_update_note", "cross-tenant", True, ProbeStatus.ERROR, detail=err)
    b = ctx.users["B"]
    bnid = b.extra.get("note_id") or _create_note(ctx, b.token, "B2", "B2 body")[0]
    if not bnid:
        return _mk("crosstenant_update_note", "cross-tenant", True, ProbeStatus.ERROR,
                   detail="no B note to attack")
    s = ctx.session()
    r = s.put(_url(ctx, f"/notes/{bnid}"), headers=_bearer(ctx.users["A"].token),
              json={"title": "HACKED", "body": "HACKED"}, timeout=ctx.timeout_s)
    denied = r.status_code in (403, 404)
    # verify B's note is unchanged
    r2 = s.get(_url(ctx, f"/notes/{bnid}"), headers=_bearer(b.token), timeout=ctx.timeout_s)
    unchanged = r2.status_code == 200 and "HACKED" not in r2.text
    status = ProbeStatus.PASS if (denied and unchanged) else ProbeStatus.FAIL
    return _mk("crosstenant_update_note", "cross-tenant", True, status,
               expected="A editing B's note -> denied AND B's note unchanged",
               observed=f"put={r.status_code}, unchanged={unchanged}",
               requests_=[_rec("PUT", f"/notes/{bnid}", "A", r.status_code),
                          _rec("GET", f"/notes/{bnid}", "B", r2.status_code)])


def crosstenant_delete_note(ctx: ProbeContext) -> ProbeResult:
    err = _ensure_users(ctx)
    if err:
        return _mk("crosstenant_delete_note", "cross-tenant", True, ProbeStatus.ERROR, detail=err)
    b = ctx.users["B"]
    bnid, code = _create_note(ctx, b.token, "B-del", "B-del body")
    if not bnid:
        return _mk("crosstenant_delete_note", "cross-tenant", True, ProbeStatus.ERROR,
                   detail=f"could not create B note (code {code})")
    s = ctx.session()
    r = s.delete(_url(ctx, f"/notes/{bnid}"), headers=_bearer(ctx.users["A"].token), timeout=ctx.timeout_s)
    denied = r.status_code in (403, 404)
    r2 = s.get(_url(ctx, f"/notes/{bnid}"), headers=_bearer(b.token), timeout=ctx.timeout_s)
    still_there = r2.status_code == 200
    status = ProbeStatus.PASS if (denied and still_there) else ProbeStatus.FAIL
    return _mk("crosstenant_delete_note", "cross-tenant", True, status,
               expected="A deleting B's note -> denied AND note still exists for B",
               observed=f"delete={r.status_code}, still_there={still_there}",
               requests_=[_rec("DELETE", f"/notes/{bnid}", "A", r.status_code),
                          _rec("GET", f"/notes/{bnid}", "B", r2.status_code)])


def crosstenant_list_isolation(ctx: ProbeContext) -> ProbeResult:
    err = _ensure_users(ctx)
    if err:
        return _mk("crosstenant_list_isolation", "cross-tenant", True, ProbeStatus.ERROR, detail=err)
    a, b = ctx.users["A"], ctx.users["B"]
    marker = f"Bmarker-{secrets.token_hex(4)}"
    _create_note(ctx, b.token, marker, marker)
    s = ctx.session()
    r = s.get(_url(ctx, "/notes"), headers=_bearer(a.token), timeout=ctx.timeout_s)
    leaked = r.status_code == 200 and marker in r.text
    status = ProbeStatus.FAIL if leaked else (ProbeStatus.PASS if r.status_code == 200 else ProbeStatus.ERROR)
    return _mk("crosstenant_list_isolation", "cross-tenant", True, status,
               expected="A's list excludes B's notes",
               observed=f"status {r.status_code}, B_marker_in_A_list={leaked}",
               requests_=[_rec("GET", "/notes", "A", r.status_code)],
               detail="LEAK: A's list contained B's note" if leaked else "")


def authz_matrix_notes(ctx: ProbeContext) -> ProbeResult:
    """owner/other/anon x GET/PUT/DELETE truth table on one of A's notes."""
    err = _ensure_users(ctx)
    if err:
        return _mk("authz_matrix_notes", "authz-matrix", True, ProbeStatus.ERROR, detail=err)
    a, b = ctx.users["A"], ctx.users["B"]
    nid, code = _create_note(ctx, a.token, "A-authz", "A-authz body")
    if not nid:
        return _mk("authz_matrix_notes", "authz-matrix", True, ProbeStatus.ERROR,
                   detail=f"could not create A note (code {code})")
    s = ctx.session()
    checks, log = [], []
    # owner: allowed
    for m, fn in (("GET", s.get), ("PUT", lambda u, **k: s.put(u, json={"title": "x", "body": "y"}, **k))):
        r = fn(_url(ctx, f"/notes/{nid}"), headers=_bearer(a.token), timeout=ctx.timeout_s)
        checks.append(("owner", m, r.status_code, r.status_code in (200, 204)))
        log.append(_rec(m, f"/notes/{nid}", "owner", r.status_code))
    # other (B): denied
    for m, fn in (("GET", s.get),
                  ("PUT", lambda u, **k: s.put(u, json={"title": "x"}, **k)),
                  ("DELETE", s.delete)):
        r = fn(_url(ctx, f"/notes/{nid}"), headers=_bearer(b.token), timeout=ctx.timeout_s)
        checks.append(("other", m, r.status_code, r.status_code in (403, 404)))
        log.append(_rec(m, f"/notes/{nid}", "other", r.status_code))
    # anon: denied
    for m, fn in (("GET", s.get), ("DELETE", s.delete)):
        r = fn(_url(ctx, f"/notes/{nid}"), timeout=ctx.timeout_s)
        checks.append(("anon", m, r.status_code, r.status_code in (401, 403)))
        log.append(_rec(m, f"/notes/{nid}", "anon", r.status_code))
    failures = [c for c in checks if not c[3]]
    status = ProbeStatus.PASS if not failures else ProbeStatus.FAIL
    return _mk("authz_matrix_notes", "authz-matrix", True, status,
               expected="owner allowed; other 403/404; anon 401/403",
               observed=f"{len(checks)-len(failures)}/{len(checks)} correct; failures={failures}",
               requests_=log)


def authspoof_absent_token(ctx: ProbeContext) -> ProbeResult:
    s = ctx.session()
    r = s.get(_url(ctx, "/notes"), timeout=ctx.timeout_s)  # no Authorization header
    ok = r.status_code in (401, 403)
    return _mk("authspoof_absent_token", "auth-spoof", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="no-token request -> 401/403", observed=str(r.status_code),
               requests_=[_rec("GET", "/notes", "no-token", r.status_code)],
               detail="accepted a request with no credential" if not ok else "")


def authspoof_forged_token(ctx: ProbeContext) -> ProbeResult:
    """Tampered/wrong-key/alg:none style forged bearer tokens must be rejected."""
    err = _ensure_users(ctx)
    if err:
        return _mk("authspoof_forged_token", "auth-spoof", True, ProbeStatus.ERROR, detail=err)
    s = ctx.session()
    forged = [
        # alg:none unsigned JWT with an admin-ish claim
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhdHRhY2tlciIsImVtYWlsIjoiYUBiLmMifQ.",
        # random junk bearer
        secrets.token_urlsafe(40),
        # a real token with the last chars mutated (tampered signature)
        (ctx.users["A"].token or "x")[:-6] + "AAAAAA",
    ]
    results, log = [], []
    for i, t in enumerate(forged):
        r = s.get(_url(ctx, "/notes"), headers=_bearer(t), timeout=ctx.timeout_s)
        accepted = r.status_code == 200
        results.append(accepted)
        log.append(_rec("GET", "/notes", f"forged#{i}", r.status_code))
    any_accepted = any(results)
    return _mk("authspoof_forged_token", "auth-spoof", True,
               ProbeStatus.FAIL if any_accepted else ProbeStatus.PASS,
               expected="all forged tokens -> rejected (not 200)",
               observed=f"accepted_any={any_accepted}", requests_=log,
               detail="a forged token was accepted as valid" if any_accepted else "")


def authspoof_malformed_token(ctx: ProbeContext) -> ProbeResult:
    s = ctx.session()
    r = s.get(_url(ctx, "/notes"), headers={"Authorization": "Bearer !!!not-a-token!!!"},
              timeout=ctx.timeout_s)
    ok = r.status_code in (401, 403)
    return _mk("authspoof_malformed_token", "auth-spoof", False,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="malformed token -> 401/403", observed=str(r.status_code),
               requests_=[_rec("GET", "/notes", "malformed", r.status_code)])


# ordered list the runner uses
PROBES = [
    live_root,
    happy_signup_login,
    happy_create_read_own_note,
    crosstenant_read_note_by_id,
    crosstenant_update_note,
    crosstenant_delete_note,
    crosstenant_list_isolation,
    authz_matrix_notes,
    authspoof_absent_token,
    authspoof_forged_token,
    authspoof_malformed_token,
]
