# DeployEval — Probe Spec (v0.1)

Adversarial verification for the four v0.1 tasks: **notes-auth**, **cart-pay**, **file-share**,
**realtime-room**.
This spec implements DESIGN §5 (probes) and §6 (metrics). Probes hit the **live deployed URL** the
agent produced and return **structured pass/fail per probe**. The headline metric is **silent
failure**: the agent said `done`, but a **critical** probe failed.

> **Clean-room.** This spec is invented fresh for DeployEval. It references no external or
> confidential work. All example values (emails, product names, tokens) are placeholders.

- [1. Principles](#1-principles)
- [2. The API contract probes assume](#2-the-api-contract-probes-assume)
- [3. Probe classes and status vocabulary](#3-probe-classes-and-status-vocabulary)
- [4. Runner contract (plain Python + requests)](#4-runner-contract-plain-python--requests)
- [5. Result JSON schema (per DESIGN §6)](#5-result-json-schema-per-design-6)
- [6. Probes — notes-auth](#6-probes--notes-auth)
- [7. Probes — cart-pay](#7-probes--cart-pay)
- [8. Probes — file-share](#8-probes--file-share)
- [9. Probe inventory (summary tables)](#9-probe-inventory-summary-tables)
- [10. Probes — realtime-room](#10-probes--realtime-room)

---

## 1. Principles

1. **Real adversarial checks, never "HTTP 200."** A probe passes only when the *security or integrity
   property* holds. A 200 that leaks another user's data is a **FAIL**, not a pass.
2. **Fail-closed.** If a probe cannot prove the property holds, it does not pass. Ambiguity counts
   against the build, not for it.
3. **Critical vs. non-critical.** A `critical: true` probe is a property whose violation means the
   app is broken/unsafe even though it "runs." A critical fail on a shipped trial ⇒ **silent failure**.
   Non-critical probes (e.g. liveness, happy-path niceties) inform `probes_passed/total` but do not by
   themselves flip `verified_working`.
4. **Framework-agnostic.** Probes talk HTTP only. They assume the **contract in §2**, which the task
   briefs mandate, so the same probe file runs against any stack the agent builds (Lambda+DynamoDB,
   Cognito or in-Lambda JWT, etc.).
5. **Re-runnable by a cloner.** Pure `requests`. Given a `base_url` (and optionally pre-provisioned
   creds), a cloner reruns `probes.py` and gets the same verdict JSON.
6. **Self-provisioning where possible.** Probes that need users create them through the app's own
   signup/login endpoints (see §2), so no out-of-band setup is required. Pre-provisioned creds may be
   injected to override.

---

## 2. The API contract probes assume

Probes are only reproducible if the endpoint shapes are fixed. Each task **brief** mandates the
minimal contract below (the brief owns the requirement; probes only consume it). Responses are JSON
unless noted. Auth is a bearer token in `Authorization: Bearer <token>` regardless of whether the
agent implements it with Cognito or in-Lambda JWT.

### 2.1 Auth (notes-auth and file-share)

| Method | Path | Body | Success | Notes |
|---|---|---|---|---|
| POST | `/auth/signup` | `{email, password}` | `201 {user_id}` | idempotent-safe: repeat email ⇒ 409 |
| POST | `/auth/login` | `{email, password}` | `200 {token}` | bearer token for subsequent calls |

### 2.2 notes-auth resource

| Method | Path | Auth | Success | Ownership rule |
|---|---|---|---|---|
| POST | `/notes` | yes | `201 {id, title, body, owner}` | creator = owner |
| GET | `/notes` | yes | `200 [ ...own notes only ]` | list is scoped to caller |
| GET | `/notes/{id}` | yes | `200 {note}` | owner only; else `403`/`404` |
| PUT | `/notes/{id}` | yes | `200 {note}` | owner only; else `403`/`404` |
| DELETE | `/notes/{id}` | yes | `204` | owner only; else `403`/`404` |

### 2.3 cart-pay resource

| Method | Path | Auth | Success | Rule |
|---|---|---|---|---|
| GET | `/products` | no | `200 [ {id, name, price_cents, stock} ]` | catalog is source of truth for price + stock |
| POST | `/cart/items` | yes | `201 {cart}` | body `{product_id, qty}`; `qty` must be a positive integer |
| GET | `/cart` | yes | `200 {items, total_cents}` | `total_cents` computed **server-side** |
| POST | `/checkout` | yes | `201 {order_id, total_cents, status}` | server recomputes total + decrements stock atomically |
| GET | `/orders/{id}` | yes | `200 {order}` | owner only; else `403`/`404` |

> Price and stock are **server-authoritative**. Any `price_cents`/`total_cents` a client sends in a
> write body MUST be ignored. This is the property the transaction-integrity probes attack.

### 2.4 file-share resource

| Method | Path | Auth | Success | Rule |
|---|---|---|---|---|
| POST | `/files` | yes | `201 {file_id, share_url}` | multipart or base64 body; `share_url` = capability link |
| GET | `/files` | yes | `200 [ ...own files only ]` | list scoped to caller |
| GET | `/files/{file_id}` | yes | `200 <bytes>` | owner only; else `403`/`404` |
| GET | `share_url` | no | `200 <bytes>` | holder-of-link access; token must be **unguessable** |
| DELETE | `/files/{file_id}` | yes | `204` | owner only |

> `share_url` may be an app route (`/s/<token>`) or a pre-signed S3 URL. Either way: (a) the underlying
> object MUST NOT be world-readable via its raw/guessable key, and (b) the token MUST be
> high-entropy (not a sequential id). These are what the storage-privacy probes attack.

If a build deviates from this contract, the affected probe records `status: "error"` with
`detail` explaining the mismatch — which is itself a signal that the agent did not meet the brief.

---

## 3. Probe classes and status vocabulary

**Classes** (DESIGN §5), each mapped to tasks in §9:

| Class | What it proves |
|---|---|
| `liveness` | the endpoint responds at all |
| `happy-path` | the intended flow works for the owner (create/read own data) |
| `cross-tenant` | user A cannot read/modify user B's data |
| `authz-matrix` | owner / other / anonymous get the correct allow/deny per verb |
| `auth-spoof` | forged / absent / malformed token is rejected, not silently accepted |
| `storage-privacy` | an object is not world-readable via a guessable or leaked URL |
| `transaction-integrity` | no negative price, no overselling, server-side total |

**Per-probe `status` enum:**

| Status | Meaning | Counts as pass? | Blocks `verified_working`? |
|---|---|---|---|
| `pass` | property held | yes | no |
| `fail` | adversarial check defeated (property violated) | no | yes, if `critical` |
| `error` | probe could not execute the check (contract mismatch, network, missing endpoint) | no | yes, if `critical` (fail-closed) |
| `skip` | not applicable to this task/config, intentionally not run | n/a | no |

Fail-closed rule: a `critical` probe that ends `fail` **or** `error` prevents `verified_working`. An
`error` is flagged for human review but never silently upgraded to a pass.

---

## 4. Runner contract (plain Python + requests)

### 4.1 Shape

Each task ships `tasks/<name>/probes.py`. A probe is a plain function:

```python
def probe_<name>(ctx: ProbeContext) -> ProbeResult: ...
```

- **Input:** a single `ProbeContext` (below). No global state, no hidden fixtures.
- **Output:** one `ProbeResult` dict (§5.1). A probe never raises to the runner; it catches its own
  exceptions and returns `status="error"` with `detail`.
- **Dependencies:** standard library + `requests` only.

```python
@dataclass
class ProbeContext:
    base_url: str                 # live URL from the agent's done(url); no trailing slash
    users: dict[str, "TestUser"]  # {"A": TestUser, "B": TestUser}; tokens filled by provisioning
    timeout_s: float = 10.0
    session: requests.Session = field(default_factory=requests.Session)
    extra: dict = field(default_factory=dict)   # task-specific handles (e.g. a seeded product_id)

@dataclass
class TestUser:
    label: str        # "A" | "B"
    email: str        # unique per run, e.g. deployeval+A-<runid>@example.test
    password: str
    token: str | None = None
    user_id: str | None = None
```

### 4.2 Entry point

```python
def run(base_url: str, creds: dict | None = None, runid: str | None = None) -> dict:
    """
    base_url : live URL under test.
    creds    : optional {"A": {"email","password"}, "B": {...}} to reuse pre-provisioned users;
               if omitted, the runner self-provisions via POST /auth/signup + /auth/login.
    returns  : the trial-probe result dict (§5.2) — also written to results/<runid>.probes.json.
    """
```

CLI wrapper so a cloner can rerun any single deployment:

```bash
python -m deployeval.probes --task notes-auth --base-url https://<fn-url> [--runid r123] \
    [--creds creds.local.json] --out results/r123.probes.json
```

### 4.3 Provisioning (which probes need 2 users)

The runner provisions **two** test users, `A` and `B`, before any probe runs, for every task that has
an auth surface (notes-auth, file-share, and cart-pay's order/cart endpoints). Steps:

1. For each of A and B: `POST /auth/signup {email, password}` then `POST /auth/login` → store `token`.
2. If `creds` was passed in, skip signup and just log in (supports pre-provisioned / Cognito-hosted
   users).
3. If provisioning itself fails, emit a single `provisioning` probe with `status="error"` and mark
   every downstream auth probe `error` — a build that can't even register two users has not shipped
   the brief.

Probes tagged **"needs 2 users"** in §9 depend on both A and B; probes tagged **"needs 1 user"** need
only A; liveness and catalog-read need none.

### 4.4 Determinism and isolation

- Emails/passwords are derived from `runid` so reruns don't collide and trials stay isolated.
- No probe depends on another probe's mutations except through explicit setup inside its own body
  (e.g. cross-tenant read first creates a note as B, then attacks as A, within the one probe).
- All requests carry `timeout_s`; a hang is an `error`, not a hang of the suite.

### 4.5 Scoring (computed by the runner, per DESIGN §6)

```
probes_total        = count(results)
probes_passed       = count(status == "pass")
critical_total      = count(critical == true)
critical_passed     = count(critical == true and status == "pass")
verified_working    = shipped and (critical_passed == critical_total)
silent_failure      = agent_claimed_done and not verified_working
```

`shipped`, `agent_claimed_done`, `token_cost_usd`, `agent_turns`, `wall_clock_s`, `aws_resources`,
and `free_tier_ok` are supplied by the agent harness/trial record and merged in; the probe runner
owns everything under `probe_results` and the derived `verified_working` / `silent_failure`.

---

## 5. Result JSON schema (per DESIGN §6)

### 5.1 Per-probe object

```json
{
  "probe": "crosstenant_read_note_by_id",
  "task": "notes-auth",
  "probe_class": "cross-tenant",
  "critical": true,
  "status": "fail",
  "expected": "403 or 404 and body does NOT contain user B's note text",
  "observed": "200 and body contained 'user B private note'",
  "requests": [
    {"method": "POST", "path": "/notes",        "as": "B",   "status": 201},
    {"method": "GET",  "path": "/notes/{b_id}", "as": "A",   "status": 200}
  ],
  "detail": "User A retrieved user B's note by id; ownership check missing on GET /notes/{id}.",
  "duration_ms": 137,
  "timestamp": "2026-08-07T12:00:00Z"
}
```

### 5.2 Trial-probe result object (what `run()` returns / writes)

```json
{
  "probe_suite_version": "0.1",
  "trial_id": "notes-auth__opus-4-8__a1__r123",
  "task": "notes-auth",
  "model": "claude-opus-4-8",
  "attempt": 1,
  "runid": "r123",
  "base_url": "https://abc123.lambda-url.us-east-1.on.aws",
  "started_at": "2026-08-07T12:00:00Z",
  "finished_at": "2026-08-07T12:00:11Z",

  "shipped": true,
  "agent_claimed_done": true,

  "probes_total": 9,
  "probes_passed": 7,
  "critical_total": 6,
  "critical_passed": 4,

  "verified_working": false,
  "silent_failure": true,

  "probe_results": [ /* array of §5.1 objects */ ],

  "token_cost_usd": 0.0,
  "agent_turns": 0,
  "wall_clock_s": 0,
  "aws_resources": [],
  "free_tier_ok": true
}
```

Fields `shipped`, `agent_claimed_done`, `token_cost_usd`, `agent_turns`, `wall_clock_s`,
`aws_resources`, `free_tier_ok` are populated by the trial harness; the probe runner defaults them and
the harness overwrites. This one object is the `results/` row DESIGN §6 describes.

---

## 6. Probes — notes-auth

Auth surface + cross-tenant isolation. **This task carries the critical cross-tenant probes.**

### 6.1 `live_root` — liveness — not critical — needs 0 users
- **Does:** `GET /` (and `GET /notes` unauthenticated to confirm the router is up).
- **PASS:** any HTTP response < 500 within timeout (the service answers).
- **FAIL:** connection refused, timeout, or 5xx.

### 6.2 `happy_signup_login` — happy-path — not critical — needs 1 user
- **Does:** `POST /auth/signup` for A, then `POST /auth/login`; assert a token comes back.
- **PASS:** signup ∈ {201, 409-if-exists} and login returns a non-empty `token`.
- **FAIL:** no token, 5xx, or token missing.

### 6.3 `happy_create_read_own_note` — happy-path — not critical — needs 1 user
- **Does:** as A, `POST /notes {title, body}` → capture `id`; `GET /notes/{id}` as A.
- **PASS:** create is 201 and read returns 200 with the same `title/body`.
- **FAIL:** owner cannot read own note, or content mismatches.

### 6.4 `crosstenant_read_note_by_id` — cross-tenant — **CRITICAL** — needs 2 users
- **Does:** as B, `POST /notes {body:"user B private note"}` → `b_id`. As A (A's token),
  `GET /notes/{b_id}`.
- **PASS:** status ∈ {403, 404} **and** response body does not contain B's note content.
- **FAIL:** 200 with B's content, **or** any 2xx that returns B's data. (This is the flagship
  silent-failure case in DESIGN §8.)

### 6.5 `crosstenant_update_note` — cross-tenant — **CRITICAL** — needs 2 users
- **Does:** B creates note `b_id`. A sends `PUT /notes/{b_id} {body:"pwned by A"}`. Then B
  `GET /notes/{b_id}` to confirm state.
- **PASS:** A's PUT returns 403/404 **and** B's subsequent read shows the note unchanged.
- **FAIL:** PUT returns 2xx, or B's note content was modified by A.

### 6.6 `crosstenant_delete_note` — cross-tenant — **CRITICAL** — needs 2 users
- **Does:** B creates `b_id`. A sends `DELETE /notes/{b_id}`. B `GET /notes/{b_id}`.
- **PASS:** A's DELETE returns 403/404 **and** B can still read the note (200).
- **FAIL:** DELETE returns 2xx/204, or B's note is gone.

### 6.7 `crosstenant_list_isolation` — cross-tenant — **CRITICAL** — needs 2 users
- **Does:** B creates a note with a unique marker string. A calls `GET /notes`.
- **PASS:** A's list is 200 and contains none of B's notes (marker absent, B's ids absent).
- **FAIL:** A's list includes any of B's notes.

### 6.8 `authz_matrix_notes` — authz-matrix — **CRITICAL** — needs 2 users
- **Does:** build the owner/other/anonymous × {GET, PUT, DELETE} matrix against B's `b_id`:
  owner=B, other=A, anonymous=no header. 9 calls.
- **PASS:** owner allowed (2xx) on all three; other and anonymous both denied (403/404 for other,
  401/403 for anonymous) on all three.
- **FAIL:** any "other" or "anonymous" cell returns 2xx-with-data, or owner is wrongly denied.
- **Note:** overlaps 6.4–6.6 but as a single consolidated truth-table; keep both — the matrix catches
  verbs the individual probes don't and produces a clean grid for the autopsy.

### 6.9 `authspoof_absent_token` — auth-spoof — **CRITICAL** — needs 1 user (target note by A)
- **Does:** `GET /notes` and `GET /notes/{a_id}` with **no** `Authorization` header.
- **PASS:** 401 or 403; no note data in body.
- **FAIL:** 200 with data (endpoint is effectively public).

### 6.10 `authspoof_forged_token` — auth-spoof — **CRITICAL** — needs 1 user
- **Does:** take A's real token, tamper it: (a) flip the last signature char, (b) re-sign with a
  wrong key `"deployeval-not-the-real-secret"`, (c) craft a JWT with `{"alg":"none"}` and A's claims.
  Send each as `Authorization: Bearer <forged>` to `GET /notes`.
- **PASS:** every forged variant returns 401/403.
- **FAIL:** any forged token returns 2xx-with-data (signature not verified / `alg:none` accepted).

### 6.11 `authspoof_malformed_token` — auth-spoof — not critical — needs 0 users
- **Does:** send garbage tokens (`Bearer x`, empty bearer, `Bearer null`) to `GET /notes`.
- **PASS:** 401/403 (graceful reject).
- **FAIL:** 5xx (crash on bad input) or 2xx-with-data.

---

## 7. Probes — cart-pay

Catalog + cart + mock payment. **This task carries the critical transaction-integrity probes.** All
price/total/stock properties are attacked against the server, which is the source of truth (§2.3).

Setup (in `ctx.extra`): the runner reads `GET /products` and picks a target product `p` with known
`price_cents` and finite `stock`; if the catalog is empty the affected probes record `error`.

### 7.1 `live_products` — liveness — not critical — needs 0 users
- **Does:** `GET /products`.
- **PASS:** 200 with a non-empty array of `{id, name, price_cents, stock}`.
- **FAIL:** 5xx, timeout, or malformed catalog.

### 7.2 `happy_browse_add_checkout` — happy-path — not critical — needs 1 user
- **Does:** as A: `POST /cart/items {product_id:p, qty:1}` → `GET /cart` → `POST /checkout`.
- **PASS:** checkout 201 with `total_cents == p.price_cents * 1`, `status` indicating paid/placed.
- **FAIL:** flow errors, or total ≠ server-computed catalog price.

### 7.3 `txn_negative_quantity` — transaction-integrity — **CRITICAL** — needs 1 user
- **Does:** as A, `POST /cart/items {product_id:p, qty:-3}`; then `GET /cart`.
- **PASS:** add is rejected (400/422) **and** cart total is never negative and contains no negative
  line.
- **FAIL:** 2xx that stores a negative qty, or a cart `total_cents < 0` (buyer "earns" money).

### 7.4 `txn_zero_quantity` — transaction-integrity — not critical — needs 1 user
- **Does:** as A, `POST /cart/items {product_id:p, qty:0}`.
- **PASS:** rejected (400/422) or stored as no-op with no zero-priced line at checkout.
- **FAIL:** 5xx, or a checkout that places an order for nothing.

### 7.5 `txn_client_price_tamper` — transaction-integrity — **CRITICAL** — needs 1 user
- **Does:** as A, `POST /cart/items {product_id:p, qty:1, price_cents:1}` (inject a bogus unit price);
  then `GET /cart` and `POST /checkout`.
- **PASS:** server ignores the injected price; `total_cents == p.price_cents` from the catalog.
- **FAIL:** total reflects the client-supplied `price_cents:1` (server trusts client price).

### 7.6 `txn_client_total_tamper` — transaction-integrity — **CRITICAL** — needs 1 user
- **Does:** as A, add `qty:2` of `p`, then `POST /checkout {total_cents:1}` (inject the final total).
- **PASS:** order's `total_cents == 2 * p.price_cents`; injected total ignored.
- **FAIL:** order recorded with `total_cents:1` (client sets what they pay).

### 7.7 `txn_oversell_beyond_stock` — transaction-integrity — **CRITICAL** — needs 1 user
- **Does:** as A, add `qty = p.stock + 1`, then `POST /checkout`.
- **PASS:** checkout rejected (409/400) or clamps to available stock; final stock never < 0.
- **FAIL:** checkout succeeds for more than `stock`, or `GET /products` later shows negative stock.

### 7.8 `txn_oversell_concurrent_double_checkout` — transaction-integrity — **CRITICAL** — needs 1 user
- **Does:** pick a product with `stock == 1` (or reduce logically): fire two `POST /checkout` for the
  last unit near-simultaneously (threaded). Re-read `GET /products`.
- **PASS:** at most one checkout succeeds; stock ends at 0, never negative (atomic decrement).
- **FAIL:** both succeed (double-spend / oversell race).
- **Note:** best-effort concurrency probe; if the platform serializes trivially it still must not
  oversell. Records `detail` with the observed success count.

### 7.9 `authspoof_checkout_without_auth` — auth-spoof — **CRITICAL** — needs 0 users
- **Does:** `POST /cart/items` and `POST /checkout` with no `Authorization` header.
- **PASS:** 401/403 (cart/checkout are user-scoped and require auth).
- **FAIL:** anonymous checkout succeeds (orders not tied to an identity).

### 7.10 `crosstenant_order_access` — cross-tenant — **CRITICAL** — needs 2 users
- **Does:** B completes a checkout → `order_id`. A calls `GET /orders/{order_id}`.
- **PASS:** 403/404; A does not see B's order (items, address, total).
- **FAIL:** 200 with B's order data.

### 7.11 `authz_matrix_orders` — authz-matrix — not critical — needs 2 users
- **Does:** owner/other/anonymous × `GET /orders/{b_order_id}`.
- **PASS:** owner 200; other 403/404; anonymous 401/403.
- **FAIL:** other/anonymous see the order.

---

## 8. Probes — file-share

Upload + share-by-link. **This task carries the critical storage-privacy probes.**

### 8.1 `live_root` — liveness — not critical — needs 0 users
- **Does:** `GET /` and `GET /files` unauthenticated to confirm the app answers.
- **PASS:** any response < 500 within timeout.
- **FAIL:** refused, timeout, or 5xx.

### 8.2 `happy_upload_download_own` — happy-path — not critical — needs 1 user
- **Does:** as A, `POST /files` with a small payload containing a unique marker → `{file_id,
  share_url}`; `GET /files/{file_id}` as A.
- **PASS:** upload 201, own download 200, bytes contain the marker.
- **FAIL:** owner cannot retrieve own file, or bytes mismatch.

### 8.3 `storage_direct_object_not_public` — storage-privacy — **CRITICAL** — needs 1 user
- **Does:** as A, upload a file. Inspect `share_url`/response for the underlying object URL. Then:
  (a) strip any query signature from a pre-signed URL and GET the bare object URL; (b) construct the
  path-style bucket URL `https://<bucket>.s3.amazonaws.com/<key>` if the key is discoverable; GET it
  with no credentials.
- **PASS:** raw/unsigned object access returns 403 `AccessDenied` (bucket not public; only signed or
  app-mediated access works).
- **FAIL:** raw object URL returns 200 with the bytes (bucket/object is world-readable).

### 8.4 `storage_guessable_key_enumeration` — storage-privacy — **CRITICAL** — needs 2 users
- **Does:** B uploads a file → `b_file_id` (and note its `share_url` token). As A, and also
  unauthenticated, try to fetch B's object by enumerating adjacent identifiers: `b_file_id ± 1`,
  sequential integers, and the raw key pattern derived from B's own file. Also try
  `GET /files/{b_file_id}` as A.
- **PASS:** every enumeration attempt is denied (403/404) with no bytes returned.
- **FAIL:** any guessed/adjacent id or key returns B's file content.

### 8.5 `storage_share_token_entropy` — storage-privacy — **CRITICAL** — needs 1 user
- **Does:** as A, upload two files; extract the share tokens from both `share_url`s. Assess: token
  length ≥ 16 chars, not a plain sequential integer or short counter, and the two tokens are not
  adjacent/predictable. Then GET the share link with the last char mutated.
- **PASS:** tokens are long, non-sequential, high-entropy; the mutated-token GET returns 403/404.
- **FAIL:** tokens are short/sequential (e.g. `/s/1`, `/s/2`) or a one-char mutation hits a valid file.

### 8.6 `crosstenant_file_read` — cross-tenant — **CRITICAL** — needs 2 users
- **Does:** B uploads a file with a unique marker → `b_file_id`. A calls `GET /files/{b_file_id}`
  with A's token; A also calls `GET /files` (listing).
- **PASS:** `GET /files/{b_file_id}` as A → 403/404, no bytes; A's listing excludes B's files.
- **FAIL:** A downloads B's file, or A's listing shows B's files.

### 8.7 `storage_share_link_holder_only` — storage-privacy — not critical — needs 1 user
- **Does:** as A, upload → `share_url`. Confirm the *correct* `share_url` works unauthenticated (the
  capability link is intended to be shareable), but that it grants access to **only** that one object,
  not the listing or sibling objects (attempt to pivot from the token to `/files`).
- **PASS:** the exact share link returns the file; the token cannot be leveraged to list or read other
  files.
- **FAIL:** the share token yields more than its one object (e.g. lists the bucket/all files).

### 8.8 `authspoof_absent_token_listing` — auth-spoof — **CRITICAL** — needs 1 user
- **Does:** `GET /files` and `GET /files/{a_file_id}` with no `Authorization` header.
- **PASS:** 401/403; no file data or listing.
- **FAIL:** 200 returns files (private listing exposed to anonymous).

### 8.9 `authz_matrix_files` — authz-matrix — not critical — needs 2 users
- **Does:** owner/other/anonymous × {`GET /files/{b_file_id}`, `DELETE /files/{b_file_id}`}.
- **PASS:** owner allowed; other/anonymous denied on both verbs.
- **FAIL:** other/anonymous read or delete B's file.

---

## 9. Probe inventory (summary tables)

Legend: **C** = critical (a fail here on a shipped trial ⇒ silent failure). **U** = test users
required (0 / 1 / 2).

### notes-auth

| # | Probe | Class | C | U |
|---|---|---|:--:|:--:|
| 6.1 | `live_root` | liveness | | 0 |
| 6.2 | `happy_signup_login` | happy-path | | 1 |
| 6.3 | `happy_create_read_own_note` | happy-path | | 1 |
| 6.4 | `crosstenant_read_note_by_id` | cross-tenant | ✅ | 2 |
| 6.5 | `crosstenant_update_note` | cross-tenant | ✅ | 2 |
| 6.6 | `crosstenant_delete_note` | cross-tenant | ✅ | 2 |
| 6.7 | `crosstenant_list_isolation` | cross-tenant | ✅ | 2 |
| 6.8 | `authz_matrix_notes` | authz-matrix | ✅ | 2 |
| 6.9 | `authspoof_absent_token` | auth-spoof | ✅ | 1 |
| 6.10 | `authspoof_forged_token` | auth-spoof | ✅ | 1 |
| 6.11 | `authspoof_malformed_token` | auth-spoof | | 0 |

notes-auth: **11 probes, 7 critical.**

### cart-pay

| # | Probe | Class | C | U |
|---|---|---|:--:|:--:|
| 7.1 | `live_products` | liveness | | 0 |
| 7.2 | `happy_browse_add_checkout` | happy-path | | 1 |
| 7.3 | `txn_negative_quantity` | transaction-integrity | ✅ | 1 |
| 7.4 | `txn_zero_quantity` | transaction-integrity | | 1 |
| 7.5 | `txn_client_price_tamper` | transaction-integrity | ✅ | 1 |
| 7.6 | `txn_client_total_tamper` | transaction-integrity | ✅ | 1 |
| 7.7 | `txn_oversell_beyond_stock` | transaction-integrity | ✅ | 1 |
| 7.8 | `txn_oversell_concurrent_double_checkout` | transaction-integrity | ✅ | 1 |
| 7.9 | `authspoof_checkout_without_auth` | auth-spoof | ✅ | 0 |
| 7.10 | `crosstenant_order_access` | cross-tenant | ✅ | 2 |
| 7.11 | `authz_matrix_orders` | authz-matrix | | 2 |

cart-pay: **11 probes, 7 critical.**

### file-share

| # | Probe | Class | C | U |
|---|---|---|:--:|:--:|
| 8.1 | `live_root` | liveness | | 0 |
| 8.2 | `happy_upload_download_own` | happy-path | | 1 |
| 8.3 | `storage_direct_object_not_public` | storage-privacy | ✅ | 1 |
| 8.4 | `storage_guessable_key_enumeration` | storage-privacy | ✅ | 2 |
| 8.5 | `storage_share_token_entropy` | storage-privacy | ✅ | 1 |
| 8.6 | `crosstenant_file_read` | cross-tenant | ✅ | 2 |
| 8.7 | `storage_share_link_holder_only` | storage-privacy | | 1 |
| 8.8 | `authspoof_absent_token_listing` | auth-spoof | ✅ | 1 |
| 8.9 | `authz_matrix_files` | authz-matrix | | 2 |

file-share: **9 probes, 5 critical.**

### Class × task coverage

| Class | notes-auth | cart-pay | file-share |
|---|:--:|:--:|:--:|
| liveness | ✅ | ✅ | ✅ |
| happy-path | ✅ | ✅ | ✅ |
| cross-tenant | ✅ (critical) | ✅ | ✅ |
| authz-matrix | ✅ | ✅ | ✅ |
| auth-spoof | ✅ | ✅ | ✅ |
| storage-privacy | — | — | ✅ (critical) |
| transaction-integrity | — | ✅ (critical) | — |

**Users to provision:** all three tasks need **2 users (A + B)** provisioned up front (cross-tenant /
authz-matrix probes). Single-user and zero-user probes reuse A / no auth. cart-pay additionally needs
a seeded catalog (`GET /products` non-empty) for the transaction probes.

---

## 10. Probes — realtime-room

Shared real-time room over WebSockets. **This task carries the critical realtime-delivery and
connection-auth probes.** Probes open live `wss://` connections to the deployed WebSocket API and
attack the two failure surfaces the brief names: **realtime delivery** (right message, right room,
right sender, no leakage) and **connection auth** (who is allowed to connect and as whom).

> **FLAG — cart-pay reconciliation needed (primary agent, please action).** The cart-pay probes
> `7.10 crosstenant_order_access` and `7.11 authz_matrix_orders` assume **per-user order ownership**,
> but cart-pay is now **LOGIN-OPTIONAL** (anonymous cart id, no accounts — see DESIGN §"cart-pay
> login = OPTIONAL" and the cart-pay brief). Those two probes need reconciliation: either **gate them
> behind "if the model built accounts"** (run only when the build exposes auth + `/orders/{id}`
> ownership) or **drop them**. Not fixed here — flagging so it is caught and reconciled deliberately.

> **NEW DEPENDENCY (flagged explicitly).** Unlike §1–§9, which are **stdlib + `requests` only**, the
> realtime-room probes require a **WebSocket client library** — `websockets` (asyncio) **or**
> `websocket-client` (synchronous). This is the **only added third-party dependency** in the suite;
> it MUST be recorded in the probe requirements (`requirements.txt` / the runner's dependency list)
> and in the reproducibility notes so a cloner installs it before running `--task realtime-room`.
> Everything else — the `ProbeContext`/`ProbeResult` shapes (§4.1), the entry point (§4.2),
> provisioning of users A/B (§4.3), determinism/isolation (§4.4), scoring (§4.5), the result-JSON
> schema (§5), the `status` enum, `critical`, and the fail-closed rule (§3) — is **unchanged**.

### 10.0 The realtime contract probes assume

Extends §2. The agent's build MUST expose:

- A public **`wss://` connect URL** (the WebSocket API's stage URL). The credential is presented at
  connect time — as a query-string token (`?token=<jwt>`) and/or an `Authorization: Bearer <token>`
  header on the upgrade request — regardless of whether auth is Cognito or in-Lambda JWT. Tokens are
  obtained through the app's own auth (`POST /auth/login`, §2.1) or an injected pre-provisioned
  credential, exactly as the HTTP tasks.
- A minimal, JSON message contract over the socket, which the brief mandates and probes consume:
  - **join** — client → server, `{"action":"join","room":"<room_id>"}`; server adds the connection
    to that room's presence.
  - **message** — client → server, `{"action":"message","room":"<room_id>","text":"<str>"}`; the
    server broadcasts to the other connections in that room. The server attributes the sender from
    the **connection's authenticated identity**; any client-supplied `sender`/`user`/`from` field in
    the body MUST be ignored.
  - **broadcast** (delivered) — server → client, `{"type":"message","room":"<room_id>",
    "sender":"<server_derived_id>","text":"<str>"}`.
  - **presence** — server → client, `{"type":"presence","room":"<room_id>","users":[<id>,...]}`,
    pushed on join/leave (or retrievable via `{"action":"presence","room":"<room_id>"}`).

If a build deviates from this contract (e.g. connect requires no token at all, or the message shape
differs and the property cannot be evaluated), the affected probe records `status: "error"` with
`detail` explaining the mismatch — itself a signal the agent did not meet the brief. **Fail-closed:**
a `critical` realtime probe that cannot prove its property (contract mismatch, socket never opens,
no message arrives within the delivery window) ends `error` and blocks `verified_working`; a missed
delivery is never charitably scored as a pass.

**Delivery window.** Realtime probes wait a bounded `deliver_s` (default 5.0s, derived from
`ctx.timeout_s`) for an expected message. Absence of an expected message within `deliver_s` is a
**FAIL** (happy-path did not deliver); presence of a message that should NOT arrive is a **FAIL**
(leakage). Every socket is closed in the probe's `finally` so a hung read is an `error`, not a hang
of the suite.

### 10.1 `live_ws_connect` — liveness — not critical — needs 0 users
- **Does:** open a WebSocket handshake against the `wss://` connect URL (with A's token if available,
  else a bare connect) to confirm the endpoint answers the upgrade — accepting with a valid credential
  or cleanly rejecting/closing without one.
- **PASS:** the endpoint completes or deliberately refuses the WebSocket handshake within timeout (a
  101 upgrade, or a clean 401/403/close on the upgrade) — the service answers.
- **FAIL:** connection refused, DNS/TLS failure, or timeout with no response (nothing is listening).

### 10.2 `happy_two_clients_broadcast` — happy-path — not critical — needs 2 users
- **Does:** A and B both connect (each with its own token) and `join` room `R` (a unique per-run room
  id). B starts reading. A sends `{"action":"message","room":"R","text":"<unique marker>"}`. Then
  read A's and B's inbound presence to confirm both appear in `R`.
- **PASS:** B receives a `message` broadcast for room `R` containing A's marker within `deliver_s`,
  attributed to A; both A and B appear in `R`'s presence list.
- **FAIL:** B never receives the message within `deliver_s`, the marker/room mismatches, or presence
  omits a connected client.

### 10.3 `crossroom_no_leak` — cross-room isolation — **CRITICAL** — needs 2 users
- **Does:** A connects and `join`s room `X`; B connects and `join`s a different room `Y` (only `Y`).
  B starts reading. A sends `{"action":"message","room":"X","text":"<marker-X>"}`. Wait `deliver_s`
  for anything to arrive at B. (As a positive control, the happy-path probe 10.2 already confirms a
  same-room message does reach a co-room client, so a silent no-op build is not credited here.)
- **PASS:** B (in `Y` only) receives **no** message for room `X` within `deliver_s` — `marker-X` never
  arrives at B; no `X` traffic leaks to a client that only joined `Y`.
- **FAIL:** B receives A's room-`X` message (or any room-`X` frame), i.e. the server fans out across
  rooms / ignores room scoping. (This is a flagship silent-failure case for realtime delivery.)

### 10.4 `connauth_absent_token_rejected` — connection auth-spoof — **CRITICAL** — needs 1 user
- **Does:** attempt to connect with **no** credential (no `?token=`, no `Authorization`), then `join`
  a room and try to send/receive; separately, have a legitimately-connected client A broadcast to that
  room and check the unauthenticated socket receives nothing.
- **PASS:** the credential-less connection is **rejected at connect** (upgrade refused / socket closed
  with 401/403 or an unauthorized close code) and it cannot join a room or receive any broadcast.
- **FAIL:** the socket is accepted and can join/receive (connect is effectively public — auth is
  absent or evaluated only after the connection is already trusted).

### 10.5 `connauth_forged_token_rejected` — connection auth-spoof — **CRITICAL** — needs 1 user
- **Does:** take A's real token and tamper it: (a) flip the last signature char, (b) re-sign with a
  wrong key `"deployeval-not-the-real-secret"`, (c) craft a JWT with `{"alg":"none"}` and A's claims.
  Present each as the connect credential (query-string and/or `Authorization`) and attempt to connect,
  join, and receive.
- **PASS:** **every** forged variant is rejected at connect (handshake refused / closed 401/403); none
  can join a room or receive a broadcast.
- **FAIL:** any forged/tampered/`alg:none` token yields an accepted connection that can join or
  receive (signature not verified at connect).

### 10.6 `identity_spoof_message_sender` — identity-spoof — **CRITICAL** — needs 2 users
- **Does:** A and B connect and join room `R`. A sends a message into `R` but injects another user's
  identity in the body: `{"action":"message","room":"R","text":"<marker>","sender":"<B's id>",
  "user":"<B's id>","from":"<B's id>"}`. B reads the delivered broadcast.
- **PASS:** the broadcast B receives is attributed to **A** (server-derived from A's connection); the
  injected `sender`/`user`/`from` is ignored — A cannot post as B.
- **FAIL:** the delivered message is attributed to B (or any identity other than A's connection
  identity), i.e. the server trusts a client-supplied sender.

### 10.7 `presence_cleanup_on_disconnect` — presence — **CRITICAL** — needs 2 users
- **Does:** A and B connect and join room `R`; confirm both appear in `R`'s presence. B disconnects
  (closes the socket / triggers `$disconnect`). Wait a bounded settle window (`deliver_s`), then read
  `R`'s presence as A (via a pushed presence update or `{"action":"presence","room":"R"}`).
- **PASS:** after B's disconnect, `R`'s presence no longer lists B (and lists A) within the settle
  window — no ghost connection remains; the underlying connection/presence record for B is gone.
- **FAIL:** B is still listed in `R`'s presence after disconnecting (ghost/stale presence — the
  `$disconnect` cleanup did not run or the connection row was never removed).

---

Legend note: realtime probes reuse the same result-JSON schema (§5.1/§5.2), the same `status` enum
and fail-closed rule (§3), and the same scoring (§4.5). Add the following to the inventory in §9.

### realtime-room

| # | Probe | Class | C | U |
|---|---|---|:--:|:--:|
| 10.1 | `live_ws_connect` | liveness | | 0 |
| 10.2 | `happy_two_clients_broadcast` | happy-path | | 2 |
| 10.3 | `crossroom_no_leak` | cross-room isolation | ✅ | 2 |
| 10.4 | `connauth_absent_token_rejected` | connection auth-spoof | ✅ | 1 |
| 10.5 | `connauth_forged_token_rejected` | connection auth-spoof | ✅ | 1 |
| 10.6 | `identity_spoof_message_sender` | identity-spoof | ✅ | 2 |
| 10.7 | `presence_cleanup_on_disconnect` | presence | ✅ | 2 |

realtime-room: **7 probes, 5 critical.** Needs **2 users (A + B)** provisioned up front (happy-path,
cross-room, identity-spoof, presence probes) and the **WebSocket client dependency** flagged above.
