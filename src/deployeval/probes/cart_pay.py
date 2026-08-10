"""cart-pay probes (11) — see docs/PROBES.md §7 and the §2.3 API contract.

Failure surface: transaction integrity. Price and stock are SERVER-authoritative; any client-supplied
price_cents/total_cents MUST be ignored. cart-pay is login-OPTIONAL, so the two order-ownership probes
(crosstenant_order_access, authz_matrix_orders) are GATED: they only run if the app exposes auth
(signup/login succeed); otherwise they SKIP.

Contract (§2.3):
  GET  /products              -> 200 [{id,name,price_cents,stock}]     (source of truth)
  POST /cart/items {product_id,qty} -> 201 {cart}                      (qty positive int)
  GET  /cart                  -> 200 {items,total_cents}               (total server-computed)
  POST /checkout              -> 201 {order_id,total_cents,status}     (server recomputes + decrements)
  GET  /orders/{id}           -> 200 {order}                           (owner only; else 403/404)
"""

from __future__ import annotations

import secrets
import threading
from typing import Optional

from .core import ProbeContext, ProbeResult, ProbeStatus, TestUser

TASK = "cart-pay"


def _url(ctx, path): return ctx.base_url.rstrip("/") + path
def _bearer(tok): return {"Authorization": f"Bearer {tok}"} if tok else {}
def _rec(m, p, a, s): return {"method": m, "path": p, "as": a, "status": s}


def _mk(name, cls, critical, status, expected="", observed="", requests_=None, detail=""):
    return ProbeResult(probe=name, task=TASK, probe_class=cls, critical=critical, status=status,
                       expected=expected, observed=observed, requests=requests_ or [], detail=detail)


def _catalog(ctx: ProbeContext):
    """Return (list_of_products, error). Cached in ctx.extra."""
    if "products" in ctx.extra:
        return ctx.extra["products"], ""
    s = ctx.session()
    try:
        r = s.get(_url(ctx, "/products"), timeout=ctx.timeout_s)
        if r.status_code != 200:
            return None, f"/products returned {r.status_code}"
        data = r.json()
        if not isinstance(data, list) or not data:
            return None, "catalog empty or not a list"
        ctx.extra["products"] = data
        return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"catalog error: {exc!r}"


def _pick(products, min_stock=1):
    """Pick a product with finite stock >= min_stock."""
    for p in products:
        try:
            if int(p.get("stock", 0)) >= min_stock and int(p.get("price_cents", 0)) > 0:
                return p
        except (TypeError, ValueError):
            continue
    return None


def _try_auth(ctx: ProbeContext) -> Optional[str]:
    """Return a bearer token if the app supports auth, else None (login-optional task)."""
    if ctx.extra.get("_auth_tried"):
        return ctx.extra.get("_token")
    ctx.extra["_auth_tried"] = True
    s = ctx.session()
    u = f"cartpay_{secrets.token_hex(4)}@example.test"
    pw = secrets.token_urlsafe(12)
    try:
        s.post(_url(ctx, "/auth/signup"), json={"email": u, "password": pw}, timeout=ctx.timeout_s)
        r = s.post(_url(ctx, "/auth/login"), json={"email": u, "password": pw}, timeout=ctx.timeout_s)
        if r.status_code == 200 and (r.json() or {}).get("token"):
            ctx.extra["_token"] = r.json()["token"]
            return ctx.extra["_token"]
    except Exception:  # noqa: BLE001
        pass
    return None


def _add_headers(ctx):
    """Auth header if the app has auth, else empty (anonymous cart)."""
    tok = _try_auth(ctx)
    return _bearer(tok)


# ---- probes ----------------------------------------------------------------

def live_products(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("live_products", "liveness", False, ProbeStatus.FAIL,
                   expected="200 non-empty catalog", observed=err, detail=err)
    return _mk("live_products", "liveness", False, ProbeStatus.PASS,
               expected="200 non-empty catalog", observed=f"{len(products)} products")


def happy_browse_add_checkout(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("happy_browse_add_checkout", "happy-path", False, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    if not p:
        return _mk("happy_browse_add_checkout", "happy-path", False, ProbeStatus.ERROR,
                   detail="no in-stock product")
    s = ctx.session(); h = _add_headers(ctx); log = []
    r = s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": 1}, timeout=ctx.timeout_s)
    log.append(_rec("POST", "/cart/items", "A", r.status_code))
    rc = s.post(_url(ctx, "/checkout"), headers=h, json={}, timeout=ctx.timeout_s)
    log.append(_rec("POST", "/checkout", "A", rc.status_code))
    ok = rc.status_code in (200, 201)
    total_ok = ok and str(p["price_cents"]) in rc.text
    return _mk("happy_browse_add_checkout", "happy-path", False,
               ProbeStatus.PASS if (ok and total_ok) else ProbeStatus.FAIL,
               expected=f"checkout 201, total == {p['price_cents']}",
               observed=f"checkout {rc.status_code}, total_matches={total_ok}", requests_=log)


def txn_negative_quantity(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("txn_negative_quantity", "transaction-integrity", True, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    s = ctx.session(); h = _add_headers(ctx)
    r = s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": -3}, timeout=ctx.timeout_s)
    rejected = r.status_code in (400, 422)
    rc = s.get(_url(ctx, "/cart"), headers=h, timeout=ctx.timeout_s)
    total_ok = True
    if rc.status_code == 200:
        try:
            total_ok = int((rc.json() or {}).get("total_cents", 0)) >= 0
        except Exception:  # noqa: BLE001
            total_ok = True
    ok = rejected and total_ok
    return _mk("txn_negative_quantity", "transaction-integrity", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="negative qty rejected (400/422); total never < 0",
               observed=f"add={r.status_code}, total_nonneg={total_ok}",
               requests_=[_rec("POST", "/cart/items", "A", r.status_code), _rec("GET", "/cart", "A", rc.status_code)],
               detail="negative qty accepted or negative total" if not ok else "")


def txn_zero_quantity(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("txn_zero_quantity", "transaction-integrity", False, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    s = ctx.session(); h = _add_headers(ctx)
    r = s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": 0}, timeout=ctx.timeout_s)
    ok = r.status_code in (400, 422, 200, 201)  # rejected or no-op both acceptable; 5xx is fail
    return _mk("txn_zero_quantity", "transaction-integrity", False,
               ProbeStatus.PASS if ok and r.status_code < 500 else ProbeStatus.FAIL,
               expected="zero qty rejected or no-op (no 5xx)", observed=str(r.status_code),
               requests_=[_rec("POST", "/cart/items", "A", r.status_code)])


def txn_client_price_tamper(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("txn_client_price_tamper", "transaction-integrity", True, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    s = ctx.session(); h = _add_headers(ctx)
    s.post(_url(ctx, "/cart/items"), headers=h,
           json={"product_id": p["id"], "qty": 1, "price_cents": 1}, timeout=ctx.timeout_s)
    rc = s.get(_url(ctx, "/cart"), headers=h, timeout=ctx.timeout_s)
    server_price_kept = rc.status_code == 200 and str(p["price_cents"]) in rc.text and "\"total_cents\": 1" not in rc.text.replace(" ", "")
    # robust check: total should be >= catalog price, not the injected 1
    tampered = False
    if rc.status_code == 200:
        try:
            tampered = int((rc.json() or {}).get("total_cents", p["price_cents"])) < int(p["price_cents"])
        except Exception:  # noqa: BLE001
            tampered = False
    ok = not tampered
    return _mk("txn_client_price_tamper", "transaction-integrity", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected=f"server ignores injected price; total >= {p['price_cents']}",
               observed=f"cart={rc.status_code}, client_price_honored={tampered}",
               requests_=[_rec("POST", "/cart/items", "A(price=1)", 0), _rec("GET", "/cart", "A", rc.status_code)],
               detail="server trusted client price_cents" if tampered else "")


def txn_client_total_tamper(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("txn_client_total_tamper", "transaction-integrity", True, ProbeStatus.ERROR, detail=err)
    p = _pick(products, min_stock=2)
    if not p:
        return _mk("txn_client_total_tamper", "transaction-integrity", True, ProbeStatus.ERROR,
                   detail="no product with stock>=2")
    s = ctx.session(); h = _add_headers(ctx)
    s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": 2}, timeout=ctx.timeout_s)
    rc = s.post(_url(ctx, "/checkout"), headers=h, json={"total_cents": 1}, timeout=ctx.timeout_s)
    expected_total = int(p["price_cents"]) * 2
    honored_bogus = False
    if rc.status_code in (200, 201):
        try:
            honored_bogus = int((rc.json() or {}).get("total_cents", expected_total)) < expected_total
        except Exception:  # noqa: BLE001
            honored_bogus = False
    ok = not honored_bogus
    return _mk("txn_client_total_tamper", "transaction-integrity", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected=f"order total == {expected_total}; injected total ignored",
               observed=f"checkout={rc.status_code}, client_total_honored={honored_bogus}",
               requests_=[_rec("POST", "/checkout", "A(total=1)", rc.status_code)],
               detail="server trusted client total_cents" if honored_bogus else "")


def txn_oversell_beyond_stock(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("txn_oversell_beyond_stock", "transaction-integrity", True, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    stock = int(p["stock"])
    s = ctx.session(); h = _add_headers(ctx)
    s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": stock + 1}, timeout=ctx.timeout_s)
    rc = s.post(_url(ctx, "/checkout"), headers=h, json={}, timeout=ctx.timeout_s)
    # re-read stock; must never be negative
    fresh, _ = _catalog_fresh(ctx, p["id"])
    neg = fresh is not None and fresh < 0
    oversold = rc.status_code in (200, 201) and (fresh is not None and fresh < 0)
    ok = (rc.status_code in (400, 409)) or (not neg)
    return _mk("txn_oversell_beyond_stock", "transaction-integrity", True,
               ProbeStatus.PASS if ok and not oversold else ProbeStatus.FAIL,
               expected="checkout>stock rejected/clamped; stock never < 0",
               observed=f"checkout={rc.status_code}, stock_after={fresh}",
               requests_=[_rec("POST", "/checkout", f"A(qty={stock+1})", rc.status_code)],
               detail="oversold: stock went negative" if oversold else "")


def _catalog_fresh(ctx, pid):
    """Re-read one product's stock live (bypasses cache)."""
    s = ctx.session()
    try:
        r = s.get(_url(ctx, "/products"), timeout=ctx.timeout_s)
        for p in (r.json() or []):
            if p.get("id") == pid:
                return int(p.get("stock", 0)), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc!r}"
    return None, "product not found"


def txn_oversell_concurrent_double_checkout(ctx: ProbeContext) -> ProbeResult:
    products, err = _catalog(ctx)
    if err:
        return _mk("txn_oversell_concurrent_double_checkout", "transaction-integrity", True,
                   ProbeStatus.ERROR, detail=err)
    p = _pick(products, min_stock=1)
    s = ctx.session(); h = _add_headers(ctx)
    # put the last unit in the cart, then fire two checkouts near-simultaneously
    s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": 1}, timeout=ctx.timeout_s)
    results = []

    def _co():
        try:
            r = ctx.session().post(_url(ctx, "/checkout"), headers=h, json={}, timeout=ctx.timeout_s)
            results.append(r.status_code)
        except Exception:  # noqa: BLE001
            results.append(0)

    t1, t2 = threading.Thread(target=_co), threading.Thread(target=_co)
    t1.start(); t2.start(); t1.join(); t2.join()
    successes = sum(1 for c in results if c in (200, 201))
    fresh, _ = _catalog_fresh(ctx, p["id"])
    neg = fresh is not None and fresh < 0
    ok = successes <= 1 and not neg
    return _mk("txn_oversell_concurrent_double_checkout", "transaction-integrity", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="<=1 checkout succeeds on last unit; stock never < 0",
               observed=f"successes={successes}, stock_after={fresh}",
               detail=f"double-spend race: {successes} succeeded, stock={fresh}" if not ok else "")


def authspoof_forged_token_rejected(ctx: ProbeContext) -> ProbeResult:
    """Auth-spoof for cart-pay. cart-pay is login-OPTIONAL (the brief allows anonymous shopping),
    so "anonymous checkout must 401" would contradict the spec. The REAL property, only meaningful
    if the app built auth, is: a FORGED/tampered bearer token must NOT be honored as a valid user.
    If login-optional (no auth), SKIP. Uses a real product id so a 404 can't be misread as rejection."""
    if _try_auth(ctx) is None:
        return _mk("authspoof_forged_token_rejected", "auth-spoof", True, ProbeStatus.SKIP,
                   detail="app is login-optional (no auth endpoints); checkout not user-scoped")
    products, err = _catalog(ctx)
    if err:
        return _mk("authspoof_forged_token_rejected", "auth-spoof", True, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    if not p:
        return _mk("authspoof_forged_token_rejected", "auth-spoof", True, ProbeStatus.ERROR,
                   detail="no in-stock product")
    # A structurally-valid but forged JWT (real header/payload shape, garbage signature).
    forged = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
              "eyJzdWIiOiJhdHRhY2tlciIsImlhdCI6MTExMTExMTExMX0." + secrets.token_urlsafe(24))
    h = _bearer(forged)
    s = ctx.session()
    r1 = s.post(_url(ctx, "/cart/items"), headers=h, json={"product_id": p["id"], "qty": 1},
                timeout=ctx.timeout_s)
    r2 = s.post(_url(ctx, "/checkout"), headers=h, json={}, timeout=ctx.timeout_s)
    # PASS if the forged token is NOT honored as a valid identity: it must not yield a paid order.
    # Ideal is 401/403; we accept any non-2xx as "not honored", but a 2xx that creates a paid order fails.
    def _not_honored(r):
        if r.status_code not in (200, 201):
            return True
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            return False
        return not (body.get("order_id") or str(body.get("status", "")).lower() == "paid")
    ok = _not_honored(r1) and _not_honored(r2)
    return _mk("authspoof_forged_token_rejected", "auth-spoof", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="forged token not honored (no paid order); 401/403 ideal",
               observed=f"cart={r1.status_code}, checkout={r2.status_code}",
               requests_=[_rec("POST", "/cart/items", "forged", r1.status_code),
                          _rec("POST", "/checkout", "forged", r2.status_code)])


def crosstenant_order_access(ctx: ProbeContext) -> ProbeResult:
    """GATED: only runs if the app built accounts. Else SKIP (login-optional)."""
    tok = _try_auth(ctx)
    if tok is None:
        return _mk("crosstenant_order_access", "cross-tenant", True, ProbeStatus.SKIP,
                   detail="login-optional app; no per-user orders to isolate")
    # provision B, place B's order, then A tries to read it
    products, err = _catalog(ctx)
    if err:
        return _mk("crosstenant_order_access", "cross-tenant", True, ProbeStatus.ERROR, detail=err)
    p = _pick(products)
    sb = ctx.session()
    ub = f"cartpay_b_{secrets.token_hex(4)}@example.test"; pw = secrets.token_urlsafe(12)
    sb.post(_url(ctx, "/auth/signup"), json={"email": ub, "password": pw}, timeout=ctx.timeout_s)
    rb = sb.post(_url(ctx, "/auth/login"), json={"email": ub, "password": pw}, timeout=ctx.timeout_s)
    btok = (rb.json() or {}).get("token") if rb.status_code == 200 else None
    if not btok:
        return _mk("crosstenant_order_access", "cross-tenant", True, ProbeStatus.ERROR,
                   detail="could not provision second user B")
    sb.post(_url(ctx, "/cart/items"), headers=_bearer(btok), json={"product_id": p["id"], "qty": 1}, timeout=ctx.timeout_s)
    rc = sb.post(_url(ctx, "/checkout"), headers=_bearer(btok), json={}, timeout=ctx.timeout_s)
    oid = (rc.json() or {}).get("order_id") if rc.status_code in (200, 201) else None
    if not oid:
        return _mk("crosstenant_order_access", "cross-tenant", True, ProbeStatus.ERROR,
                   detail=f"B checkout failed ({rc.status_code}); no order to attack")
    ra = ctx.session().get(_url(ctx, f"/orders/{oid}"), headers=_bearer(tok), timeout=ctx.timeout_s)
    ok = ra.status_code in (403, 404)
    return _mk("crosstenant_order_access", "cross-tenant", True,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="A reading B's order -> 403/404",
               observed=f"status {ra.status_code}",
               requests_=[_rec("GET", f"/orders/{oid}", "A", ra.status_code)],
               detail="A read B's order" if not ok else "")


def authz_matrix_orders(ctx: ProbeContext) -> ProbeResult:
    """GATED like above; non-critical."""
    if _try_auth(ctx) is None:
        return _mk("authz_matrix_orders", "authz-matrix", False, ProbeStatus.SKIP,
                   detail="login-optional app; no per-user orders")
    # reuse the cross-tenant result shape lightly: anonymous read of a fabricated id must not 200
    s = ctx.session()
    r = s.get(_url(ctx, "/orders/nonexistent-" + secrets.token_hex(4)), timeout=ctx.timeout_s)
    ok = r.status_code in (401, 403, 404)
    return _mk("authz_matrix_orders", "authz-matrix", False,
               ProbeStatus.PASS if ok else ProbeStatus.FAIL,
               expected="anon order read -> 401/403/404", observed=str(r.status_code),
               requests_=[_rec("GET", "/orders/<fake>", "anon", r.status_code)])


PROBES = [
    live_products,
    happy_browse_add_checkout,
    txn_negative_quantity,
    txn_zero_quantity,
    txn_client_price_tamper,
    txn_client_total_tamper,
    txn_oversell_beyond_stock,
    txn_oversell_concurrent_double_checkout,
    authspoof_forged_token_rejected,
    crosstenant_order_access,
    authz_matrix_orders,
]
