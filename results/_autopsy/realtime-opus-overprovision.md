# Finding: realtime-room × Opus — verified-working, but over-provisioned

**Verdict:** VERIFIED (7/7 probes, 5/5 critical) — every realtime security property holds: cross-room
isolation, connect-auth (no-token + forged rejected at handshake), identity-not-spoofable (server-derived
sender), presence cleanup on disconnect. This is the hardest task in the suite and Opus got the security right.

**But `free_tier_ok = False`.** The stack included an `AWS::CloudFront::Distribution` that the brief never
asked for. The raw API Gateway WebSocket API (`wss://<id>.execute-api.us-west-2.amazonaws.com/prod`) is
fully functional on its own; Opus added CloudFront in front of it (likely for a cleaner domain). CloudFront
is a distinct CDN service with a *time-limited* free tier (1 TB/mo for 12 months, then billed), unlike the
always-free serverless core (Lambda / API Gateway / DynamoDB).

**Why we report it as free_tier_ok=False rather than allowlisting CloudFront:**
The "$0" claim is meant to rest on *always-free* serverless primitives, not a 12-month-limited CDN tier.
Opus met the functional + security bar but pulled in an unnecessary extra service — a mild
over-provisioning that a cost-conscious founder would not want by default. Honest scoring: the app works
and is secure, but it is not strictly within the minimal free-tier set. This is a second axis of "silent"
behavior: not a security hole, but a cost surprise the agent introduced without being asked and without flagging it.

Contrast: Sonnet's builds on the HTTP tasks stayed on the minimal service set (free_tier_ok=True).

**Update — this is a SHARED behavior, not Opus-specific.** Sonnet's realtime-room build did the exact
same thing: VERIFIED 7/7, 5/5 critical, and also fronted the WebSocket API with an unnecessary
`AWS::CloudFront::Distribution` (free_tier_ok=False). So on the WebSocket task, BOTH tiers (a) got the
security fully right and (b) over-provisioned identically. The only difference was cost: Sonnet $1.45
vs Opus $7.33 for the same verified-working, same-over-provisioned result — a 5x cost gap for no
functional or security difference. This reinforces the thesis from a different angle: the frontier model
was not more correct, not more frugal, just more expensive.
