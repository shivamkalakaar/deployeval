# DeployEval — Design (v0.1 — LOCKED 2026)

## ⚠ PILOT FINDING (2026) — deploy path changed
The Haiku pilot proved the loop but surfaced a hard account constraint: **public Lambda Function
URLs are blocked on this AWS account (AuthType NONE → 403, SCP-level).** A diagnostic confirmed
**API Gateway HTTP API (ApiGatewayV2) returns 200 publicly.** So:
- **Required public entry = API Gateway HTTP API (v2)** for all HTTP tasks (was "Lambda Function URLs").
- **Lambda Function URLs are now BANNED** (they 403 here). allowlist.yaml updated accordingly.
- Realtime already used ApiGatewayV2 (WebSocket) — now HTTP-API is global, so no special carve-out.
- Every task brief's "Lambda Function URLs (no API Gateway)" line must be corrected to
  "API Gateway HTTP API" before the full run. (Briefs pending this edit.)
- Also: the build agent stalled after ~10 min at the "expose public URL" step; the full-run
  orchestrator must give agents a longer budget and a clear "use API Gateway HTTP API" instruction.

## LOCKED DECISIONS (supersede any draft text below)
- **$0 constraint:** no paid API. The **agent is Claude Code itself** (Amazon-native, on Bedrock),
  the same setup as the prior 72-build study. No custom Bedrock tool-use loop to build — Claude Code
  does the design/build/deploy directly, driven by the task brief. (This replaces "Open Decision A".)
- **Multi-model = switch the Claude Code model.** `~/.claude/settings.json` exposes selectable models
  (`availableModels`, `modelOverrides`), so we run the same eval under different models at $0.
  Mechanism is a **documented semi-manual protocol**: launch/point Claude Code at model N, it runs the
  build+deploy+probe for each task, results logged. "Clone and rerun" = follow the documented steps.
- **Models for v0.1 (distinct only; ignore `[1m]` context-variant dupes):** tier ladder —
  **Claude Opus 4-8 (frontier) · Sonnet 5 (mid) · Fable-5 · Haiku 4-5 (cheap)** = 4 models. Answers
  "do you need the expensive model, or does the cheap one ship the same app?" Generational Opus ladder
  (4-6→5) is a later, optional second chart.
- **Deploy target (Open Decision B → LOCKED):** SAM/CloudFormation + **free-tier allowlist**
  (Lambda + Function URLs, DynamoDB, S3, Cognito-or-in-Lambda-JWT). One stack per trial → clean
  teardown + guaranteed $0.
- **Tasks (Open Decision C → LOCKED):** **notes-auth, cart-pay, file-share, realtime-room** (4).
  - **cart-pay login = OPTIONAL** (anonymous cart id) — its failure surface is transaction integrity,
    not identity; keeps the tasks testing distinct surfaces. (Confirmed by Shivam.)
  - **realtime-room ADDED to v0.1** (Shivam's call): shared room with live updates over WebSockets.
    Failure surface = realtime delivery + connection auth. Heaviest task (APIGW WebSocket API +
    Lambda + DynamoDB connection table); most likely to be partially-built — a fail is valid signal.
- **Scope now: 4 tasks × 4 models × 1 attempt = 16 trials.** (Shivam confirmed full scope + added
  Fable-5; no rush, quality over speed.)
- **Verification (Open Decision D → LOCKED):** automated probes are the **published metric**; plus
  one **documented manual red-team deep-dive** on **realtime-room** (Shivam probes it by hand —
  realtime/websocket auth is the subtlest surface).
- **cart-pay order probes GATED:** `crosstenant_order_access` + `authz_matrix_orders` run only IF the
  model built accounts; otherwise skipped (login-optional task). No lost signal either way.
- **Scope v0.1:** 3 tasks × 3 models × 1 attempt = **9 trials.** Multi-attempt (pass^k) later.
- **Apples-to-apples (per Shivam):** fixed prompt creates the app; the **model does all design/build/
  deploy**; identical brief + tools + probes + budget across models; only the model changes.

---

# DeployEval — Design (v0.1 draft — superseded above where they conflict)

**Question it answers, for founders:** *Can an AI coding agent take a plain-English app brief and
ship a working, secure app on the AWS free tier for $0 — and do you need a frontier model to do it,
or does a cheaper model ship the same thing?*

Two axes:
1. **Free-tier deployability** — does a real app go from brief to a live URL on AWS free tier at $0?
2. **Model tier vs. capability** — does a cheaper model (e.g. Claude Haiku) ship the same working
   app as a frontier model (e.g. Claude Opus)? If yes, that saves founders money.

The output is a **published, dated results snapshot** + a fully public, re-runnable pipeline. Anyone
clones the repo, plugs in their own AWS keys, and reproduces the numbers.

---

## 1. Unit of evaluation

One **trial** = one `(task × model × attempt)`. The pipeline runs every task on every model; each
trial is fully isolated (own resource name prefix, own transcript, torn down after). Default: 1
attempt per pair for v0.1; the schema supports N attempts so we can report consistency (pass^k) later.

`trials = tasks (4) × models (2) × attempts (1) = 8` for v0.1.

---

## 2. The agent harness — how a model goes brief → deployed app (the core)

This is the "all the way to deployment" part, and the hardest to build honestly. A model alone can't
deploy; it needs to *act* — write files, run commands, call AWS. So the harness is a **coding-agent
loop** driven by a Bedrock model:

```
for each (task, model):
  give agent: the brief, an empty working dir, an allowed toolset, the deploy rules
  loop until agent says DONE or hits the budget:
     model produces: reasoning + a tool call (write_file | run_bash | done)
     harness executes the tool, returns real output (stdout/stderr) to the model
     model reacts, iterates on errors
  capture: full transcript, all files, the live URL, all AWS resource IDs, token usage
```

- **Model access:** Bedrock **Converse API with tool use** (same account that bills the deploys).
  The model list is config-driven (add a model = one line), so "rerun when a new model drops" is real.
- **Tools given to the agent** (deliberately minimal, like a real coding agent):
  - `write_file(path, contents)` — write code into the working dir
  - `run_bash(cmd)` — run shell (aws cli, python/boto3, sam, npm, curl), captured
  - `done(url)` — declare finished with the live URL
- **The agent must deploy itself.** It writes the app + the infra and runs the deploy. We do not
  deploy for it. That is the whole point: can the *agent* ship.
- **Fairness:** identical brief, identical toolset, identical budget, identical deploy rules for every
  model. Only the model ID changes. (This mirrors the symmetric protocol from prior work, genericized.)

**Budget/guardrails per trial** (bounds cost + runaway loops):
- Max agent turns (e.g. 40) and/or max wall-clock (e.g. 20 min).
- Max tokens per trial (hard ceiling).
- If the agent exceeds budget without a working `done`, the trial is scored **fail (did not ship)** —
  which is itself signal.

**OPEN DECISION A — build the loop or use a framework?**
- (a) **Build a minimal Bedrock tool-use loop ourselves** — full control, clean cross-model
  comparison, and it's the honest "I built an agent harness" story. More work.
- (b) Use an off-the-shelf agent framework (e.g. Strands, LangChain) as the loop.
- *Lean: (a).* It's the credible, defensible build and avoids "the framework did it" ambiguity.

---

## 3. Deployment target — AWS free-tier stack (fixed, enforced)

A **fixed allowlist** of free-tier services the agent may use. Everything is provisioned as a single
**CloudFormation / AWS SAM stack** per trial, named `deployeval-<task>-<model>-<runid>`.

| Concern | Service | Free-tier basis |
|---|---|---|
| Compute | **Lambda** | 1M requests/mo always-free |
| HTTP entry | **Lambda Function URLs** (not API Gateway) | free; avoids APIGW cost surface |
| Database | **DynamoDB** | 25GB + 25 RCU/WCU always-free |
| Object storage | **S3** | 5GB (12-mo free) |
| Auth (if task needs it) | **Cognito** | 50k MAU free — OR in-Lambda JWT |
| Packaging | **CloudFormation/SAM** | free; one-command teardown |

**Why a single stack:** deterministic teardown (`delete-stack` removes everything → guarantees $0),
and free-tier enforcement (we can inspect the stack and reject any resource outside the allowlist).

**OPEN DECISION B — how strictly do we constrain the stack?**
- (a) **Require SAM/CloudFormation + allowlist** (recommended): clean teardown, enforceable free-tier,
  fair comparison. Slightly less "wild" than letting the agent do anything.
- (b) Let the agent use raw CLI/boto3 freely: more realistic, but teardown + free-tier checking get
  hard and risky (orphaned billable resources).
- *Lean: (a)* — the teardown-safety and $0-guarantee reasons are strong and defensible.

---

## 4. Task suite (the app briefs)

Each task is a `tasks/<name>/brief.md` (requirements + acceptance criteria, **no solution**) plus a
`probes.py` (the adversarial checks). v0.1 = 4 tasks, each stressing a distinct failure surface:

1. **notes-auth** — multi-user notes with login. Stresses **auth + cross-tenant isolation**.
2. **cart-pay** — catalog + cart + mock payment. Stresses **transaction integrity** (can't pay $0,
   can't order what's out of stock).
3. **file-share** — upload a file, share by link. Stresses **object-storage privacy** (can a
   stranger read someone else's file?).
4. **realtime-room** *(stretch)* — a shared room with live updates. Stresses **realtime + connection
   auth**. (Cut to 3 tasks if realtime proves flaky on free tier.)

Each brief specifies: the data model, required endpoints, the auth requirement, and **explicit
acceptance criteria** the probes will check. Briefs are generic and public — no connection to any
prior/internal work.

**OPEN DECISION C — task count + which 4.** Lean: notes-auth, cart-pay, file-share (+ realtime as
stretch). Swap any?

---

## 5. Probes — adversarial verification (the IP)

Per task, `probes.py` hits the **live deployed URL** and returns pass/fail per probe. Probes come in
classes (genericized from prior adversarial-probe work), assigned per task:

- **Liveness** — does the app load / does the endpoint respond at all.
- **Happy path** — the intended flow works (create note, read own note).
- **Cross-tenant** — user A cannot read/modify user B's data (the big one for notes-auth).
- **Authz matrix** — owner/other/anonymous get the right allow/deny on each verb.
- **Auth-spoof** — a forged/absent token is rejected (not silently accepted).
- **Storage-privacy** — a file/object is not world-readable via a guessable/leaked URL (file-share).
- **Transaction-integrity** — no negative price, no overselling, server-side enforcement (cart-pay).

**The headline metric depends on these being real adversarial checks, not "HTTP 200."** A build that
returns 200 but leaks tenant data **fails**. This is the "done vs. works" gap.

**OPEN DECISION D — automated vs. hybrid verification.**
- (a) **Fully automated probes** (recommended for a public re-runnable benchmark): anyone reruns and
  gets the same verdict. Slightly less devious than a human red-teamer.
- (b) Hybrid: automated probes + a documented manual red-team pass you run and log.
- *Lean: (a) as the published metric, with (b) as an optional documented deep-dive on one task* — so
  it's reproducible AND shows human rigor.

---

## 6. Metrics recorded (per trial → `results/`)

Each trial writes a row:
- `shipped` (bool) — agent produced a live URL within budget
- `probes_passed` / `probes_total`, and per-probe pass/fail
- `verified_working` (bool) — shipped AND all critical probes pass
- `silent_failure` (bool) — **agent said done, but a critical probe failed** (headline)
- `token_cost_usd` — Bedrock input+output tokens at published rates (see §7)
- `agent_turns`, `wall_clock_s`
- `aws_resources` — list (for teardown + free-tier audit)
- `free_tier_ok` (bool) — only allowlisted services, within limits

**Cross-model comparables:** `shipped`, `verified_working`, `silent_failure`, `token_cost_usd`.

---

## 7. Cost metering

- **Token cost:** sum input+output tokens from each Bedrock Converse response's usage, at that model's
  **published Bedrock price** (hard-coded in a small `prices.py`, public info). Report per-trial and
  per-model totals. Honest rule: if a transcript is truncated/incomplete, mark it a **floor**, never
  back-fill an estimate.
- **AWS cost:** the point is $0. We (a) restrict to the free-tier allowlist, (b) tear everything down
  same-run, (c) note that sustained free-tier usage is $0 within limits. We do not claim a dollar AWS
  figure; we claim "$0 within free-tier limits," which is the founder-relevant answer.

---

## 8. Flagship result / chart

A two-panel figure (built per the dataviz skill, validated palette):
- **Left — the "done vs. works" gap:** per model, self-reported ship rate vs. adversarially-verified
  working rate. The gap = silent-failure rate.
- **Right — cost vs. capability:** per model, verified-working rate vs. token cost per verified build.
  Story a founder cares about: *does the cheap model ship the same working app for less?*

Plus a written **transcript autopsy**: one concrete failure (e.g., "Haiku deployed notes-auth but the
cross-tenant probe read another user's notes — here's the transcript line where it skipped the
ownership check") + what it implies for model choice. This is the "model taste" signal.

---

## 9. Reproducibility, isolation, teardown, safety

- **Isolation:** unique `runid` + resource-name prefix per trial; no shared state.
- **Teardown:** `delete-stack` per trial, verified; a `teardown.py` sweeps any stack matching the
  prefix (belt-and-suspenders so nothing bills after a run).
- **Credentials:** read from the user's environment (`~/.aws/credentials` or env vars). Never in the
  repo, never in transcripts we commit. `.gitignore` blocks `.env`, `credentials*`, `*.csv` keys.
- **Confidentiality gate:** the repo is clean-room. Pre-publish grep for banned strings (internal
  codename, AWS account numbers, internal doc author, internal cost totals). Nothing from the private
  project enters this repo.
- **Committed transcripts:** scrub account IDs / ARNs before committing any sample transcript
  (`runs/` is gitignored; only a scrubbed sample goes in `examples/`).

---

## 10. What ships in v0.1 vs. later

- **v0.1 (prove it end-to-end):** the agent loop, the free-tier deploy stack, 3 tasks (notes-auth,
  cart-pay, file-share), automated probes, cost meter, 2 models (Opus vs Haiku), the chart + one
  autopsy, README + docs + tests + CI.
- **Later:** realtime task, more models (config line each), multi-attempt consistency (pass^k), the
  MCP-server + Agent-Skill wrappers (prove the resume's MCP/skills claim publicly).

---

## Open decisions to lock (summary)
- **A.** Agent loop: build our own Bedrock tool-use loop *(lean yes)* vs. framework.
- **B.** Stack: require SAM/CloudFormation + free-tier allowlist *(lean yes)* vs. free-form.
- **C.** Tasks: notes-auth + cart-pay + file-share (+ realtime stretch) — confirm/swap.
- **D.** Verification: automated probes as the published metric + optional documented human deep-dive
  *(lean this)*.
- **E.** Models for first snapshot: Claude Opus vs Claude Haiku on Bedrock — confirm, add a third?
- **F.** Scope: 3 tasks × 2 models = 6 trials for v0.1 — comfortable, or start with 2×2?
