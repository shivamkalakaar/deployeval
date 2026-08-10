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
