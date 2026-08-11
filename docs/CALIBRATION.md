# Harness calibration log

Transparent record of every change made to probes / scoring **after** trials began, so results
stay honest and reproducible. Each entry says what changed, why, and which trials were re-run.

## 2026-08-07 — cart-pay auth-spoof probe + custom-resource allowlist

Discovered while measuring the first cart-pay trial (Opus). Two harness-calibration issues, neither
a real product defect:

1. **`authspoof_checkout_without_auth` contradicted the task brief.** cart-pay is *login-optional*
   (the brief: "No user login is required to browse or shop"), yet the probe required an anonymous
   checkout to return 401/403. It fired only because a model *also* built auth endpoints, and it used
   a bogus `product_id: "x"` so a 404 ("no such product") was misread as an auth failure. The real
   security property is **"a forged/tampered credential must not be honored as a valid user."**
   - Fix: replaced with `authspoof_forged_token_rejected` — presents a structurally-valid but
     forged JWT against a *real* product; PASS iff the forged token yields no paid order (401/403
     ideal, any non-2xx accepted; a 2xx that creates a paid order fails). SKIPs when the app is
     genuinely anonymous (no auth endpoints), consistent with the login-optional contract.
   - Re-ran: all three cart-pay trials (opus, sonnet, fable) under the corrected probe.

2. **`AWS::CloudFormation::CustomResource` counted against free-tier.** A CFN custom resource is a
   deploy-time helper backed by an already-allowlisted Lambda (e.g. seeding the product catalog);
   it runs once at deploy and bills nothing ongoing. Flagging it made `free_tier_ok=False` for a
   stack that is genuinely $0.
   - Fix: allowlisted `AWS::CloudFormation::CustomResource` and the `Custom::` type prefix
     (custom resources surface as `Custom::<Name>`). `freetier_check` now honors `::`-suffixed
     prefix entries.

Decisions approved by the project owner before applying; changes are code + tests (10 passing).
Rule going forward: probes test the *security property named in the brief*, not a specific HTTP
status code, and the allowlist reflects *ongoing* billable cost, not deploy-time helpers.

## 2026-08-10 — realtime-room auth-host assumption (corrected)

Discovered while measuring realtime-room × Opus 5. The realtime probe derived the HTTP auth host
(`/auth/signup`, `/auth/login`) from the `wss://` base by string-swapping the scheme, assuming auth
lives on the WebSocket hostname. That assumption is architecturally wrong: a bare API Gateway
**WebSocket** execute-api host returns `403 Forbidden` to *all* plain-HTTP requests, so auth cannot be
served on the wss hostname without CloudFront or a custom domain. A correct, minimal free-tier build
therefore puts auth on a **separate HTTP API host**.

Consequence of the bug: the model that made the *cleanest* choice was punished. Opus 5 put auth on a
separate HTTP host (staying truly free-tier) and the probe, unable to find login, failed to get a token
so every token-dependent probe errored `401 at connect` -> a **false silent_failure**. Manual
verification confirmed Opus 5's app works end to end (signup 201, login 200 with a 211-char token, two
clients join a room and the broadcast is delivered, and all connect-auth rejections are correct).
Meanwhile Opus 4.8 and Sonnet 5 only "passed" earlier because they unified the host via CloudFront —
which the free-tier audit then flagged (`free_tier_ok=False`). So the probe rewarded over-provisioning
and penalized the correct architecture.

- Fix: `_http_base` now honors an explicit `ctx.extra["auth_base"]` (a separate HTTP auth host),
  falling back to scheme-swap only for genuinely-unified hosts. `measure` gains `--auth-url`; the
  realtime build prompt now instructs models to place auth on a separate HTTP host and report it as
  `AUTH_URL`.
- Re-ran: all three realtime-room trials (opus-5, opus-4-8, sonnet-5) under the corrected probe, each
  measured against its reported auth host.

Approved by the project owner before applying.

## 2026-08-10 — presence-cleanup probe is too brittle to auto-score (downgraded)

The final trial (realtime-room × Sonnet 4-6) auto-flagged `presence_cleanup_on_disconnect` as a
silent failure: after a client disconnected, a presence query still returned two members. Hand
verification with two DISTINCT users showed the opposite: both-connected presence returns a proper
2-person roster (`{users:[A,B]}`), and 8s after B disconnects the app actively emits
`{event:"leave", user:B}` — i.e. cleanup works and A is notified.

The probe was wrong for a response-shape reason: this build answers a steady-state `presence` query
with a `users` roster, but immediately after a disconnect it sends an *event* frame (`{event:"leave",
user}`) rather than a fresh roster. The probe's `len(users)` heuristic read a slightly-stale roster and
counted 2 -> false FAIL. This is the third realtime false-flag from an over-rigid assumption (after the
auth-host issue), confirming the module's own note that presence is a manual-review probe.

- Fix: `presence_cleanup_on_disconnect` is downgraded to **non-critical** and treats a `leave`/absence
  signal within the delivery window as PASS; when the response shape is ambiguous it returns
  `needs_human_review` rather than FAIL, so it can never fabricate a critical silent failure on its own.
- Re-scored: realtime-room × Sonnet 4-6 -> VERIFIED (cleanup confirmed by hand). This makes realtime 4/4
  verified.

Approved by the project owner (re-run-to-hand-verify was the chosen path).
