# DeployEval — Harness, Deploy Rules, Cost Metering, Teardown

This document is the operational spec for running DeployEval. It covers the run
protocol, resource naming and isolation, free-tier enforcement, cost metering,
teardown, the results schema, and the reproducibility/safety rules. Read
`DESIGN.md` first — this file implements the LOCKED DECISIONS there (especially
sections 3, 6, 7, 9).

The one fact everything below depends on:

> **The agent is Claude Code itself.** There is no custom Bedrock tool-use loop
> to build. A human runs Claude Code, points it at one model, hands it a task
> brief, and Claude Code writes the app + infra and deploys it. The "harness" is
> therefore a **documented protocol plus a handful of read-only supporting
> scripts**, not an autonomous API agent. "Clone and rerun" means: follow the
> checklist in section 1.

---

## 0. What you need before a run

- **An AWS account** with credentials in your own environment
  (`~/.aws/credentials`, an `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` env vars). Credentials are read
  from the environment only. They are **never** written into the repo, a task
  brief, a transcript we commit, or a results file. See section 7.
- **AWS SAM CLI** and the **AWS CLI v2** on PATH (`sam --version`,
  `aws --version`).
- **Python 3.11+** for the supporting scripts (`boto3`, `pyyaml`).
- **Claude Code**, able to switch between the three tier models. The models are
  selected in Claude Code's `settings.json` via `availableModels` /
  `modelOverrides` / `model`; switching is a settings change or the in-app model
  picker. On an Amazon-native (Bedrock) setup the model IDs carry an
  `anthropic.` prefix; on the first-party setup they are the bare aliases. Both
  are handled the same way here — the harness only records *which* alias ran.
- A clean region with nothing else in it, ideally a throwaway/sandbox account,
  so the free-tier audit and teardown sweep can't touch unrelated resources.

The three tier models for v0.1 (distinct only; ignore `[1m]` context-variant
duplicates):

| Tier | Model alias | Bedrock model ID |
|---|---|---|
| frontier | `claude-opus-4-8` | `anthropic.claude-opus-4-8` |
| mid | `claude-sonnet-5` | `anthropic.claude-sonnet-5` |
| cheap | `claude-haiku-4-5` | `anthropic.claude-haiku-4-5` |

---

## 1. Run protocol — one model across the three tasks

A **trial** is one `(task × model × attempt)`. A **run** is one model taken
across all three tasks (`notes-auth`, `cart-pay`, `file-share`) at one attempt
each = 3 trials. v0.1 is 3 tasks × 3 models × 1 attempt = **9 trials**.

Do the trials one at a time. Each trial is fully isolated (its own `runid`, its
own stack, its own transcript, torn down before the next). The checklist below
is per trial; repeat it for every `(task, model)` pair.

### 1.1 Per-trial checklist

**A. Pick the trial and mint a runid.**

```
task=notes-auth          # or cart-pay | file-share
model=claude-opus-4-8    # or claude-sonnet-5 | claude-haiku-4-5
runid=$(date +%Y%m%d)-$(python3 -c "import secrets;print(secrets.token_hex(3))")
prefix="deployeval-${task}-${model//./-}-${runid}"
```

`prefix` is the single string that ties the CloudFormation stack, every AWS
resource name, the transcript, and the results row together. Dots in the model
alias are replaced with hyphens so the prefix is a legal stack/resource name.
See section 2 for the full naming rule.

**B. Set the model in Claude Code.** Point Claude Code at `model` — either edit
`settings.json` (`"model": "<alias>"`) or use the in-app model picker. Confirm
the active model before proceeding; the model identity in the results row comes
from *this* choice, recorded in the run manifest, not from the transcript (the
transcript's `message.model` is unreliable — see section 4.3).

**C. Prepare the workspace.** Create an empty working directory for the trial
and record where the Claude Code session transcript will land (section 4.1):

```
mkdir -p runs/${prefix}/workdir
# runs/ is gitignored — see section 7
```

**D. Feed Claude Code the task brief + deploy rules.** In a fresh Claude Code
session whose cwd is the trial workdir, give it exactly three things, identical
across all models (fairness — DESIGN §"Apples-to-apples"):

1. the task brief: `tasks/<task>/brief.md`
2. the deploy rules: the free-tier allowlist and "one SAM/CloudFormation stack,
   named with this exact prefix, must tear down cleanly" (section 3), with the
   `prefix` value for this trial
3. the instruction to build and deploy to a live URL, then report the URL and
   the stack name

Do not help beyond this. The model does all design, build, and deploy. If it
asks clarifying questions, answer only from the brief. Copy the exact prompt
you used into `runs/${prefix}/prompt.txt` so the run is reproducible.

**E. Let it build + deploy.** Claude Code writes the app, writes the SAM
template, runs `sam build` / `sam deploy`, and iterates on errors. It must use
only allowlisted services and must name the stack `${prefix}`. When it declares
done, it should report a live URL and the stack name.

**F. Capture the artifacts.** Before touching anything, record:

- **live URL** → `runs/${prefix}/url.txt`
- **stack name** → should equal `${prefix}`; verify with
  `aws cloudformation describe-stacks --stack-name ${prefix}`
- **AWS resource IDs** → `aws cloudformation list-stack-resources --stack-name
  ${prefix} > runs/${prefix}/resources.json`
- **transcript** → copy the session JSONL for this trial into
  `runs/${prefix}/transcript.jsonl` (section 4.1 explains how to find it). Copy
  it read-only; do not edit it.
- **agent turns / wall-clock** → derivable from the transcript timestamps
  (section 4.4); note the session start/end wall time as a cross-check.

**G. Enforce the free-tier allowlist.** Run the allowlist check (section 3.3)
against the deployed stack. If it used a non-allowlisted service, the trial is
recorded `free_tier_ok=false` — that is a finding, not a reason to hide it.

```
python3 src/freetier_check.py --stack ${prefix} --template runs/${prefix}/workdir/<template>.yaml
```

**H. Run the probes against the live URL.** The probes are the published metric
(DESIGN §5). They hit the deployed URL, not the code.

```
python3 tasks/${task}/probes.py --url "$(cat runs/${prefix}/url.txt)" \
  --out runs/${prefix}/probes.json
```

Each probe returns pass/fail. `verified_working` is true only if the app
shipped AND every **critical** (SECURITY-tagged) probe passes. `silent_failure`
is true when the agent said done but a critical probe failed — the headline
metric.

**I. Meter token cost.** Parse the trial transcript at the published per-model
Bedrock rate (section 4).

```
python3 src/cost_meter.py --transcript runs/${prefix}/transcript.jsonl \
  --model ${model} --out runs/${prefix}/cost.json
```

**J. Write the results row.** Assemble the per-trial JSON row (section 6) from
the manifest, the probe output, the free-tier check, and the cost meter:

```
python3 src/write_result.py --prefix ${prefix} --out results/${prefix}.json
```

**K. Tear down.** Delete the stack for this trial and verify zero remaining
billable resources (section 5).

```
python3 src/teardown.py --prefix ${prefix} --verify
```

**L. Move to the next trial.** Repeat A–K for the next `(task, model)`.

### 1.2 Optional documented red-team deep-dive

Per DESIGN §5 / Open Decision D, in addition to the automated probes you may run
one **manual** adversarial pass on a single task (recommended: `notes-auth`
cross-tenant) and log it as prose + captured requests/responses under
`runs/<prefix>/redteam/` (scrubbed — section 7). This is the "human rigor"
supplement to the published automated metric, not a replacement for it.

### 1.3 Budget guardrails (scoring a non-ship)

DESIGN §2 keeps the runaway-cost guardrails as scoring rules, adapted to the
Claude-Code-is-the-agent reality:

- **Wall-clock ceiling** per trial (e.g. 30 min). If Claude Code hasn't produced
  a working `done(url)` by then, stop the session and record the trial as
  `shipped=false` — a "did not ship" is itself signal.
- If the model loops on the same deploy error without progress, stop and record
  `shipped=false`.
- Always run teardown (step K) even for a non-ship — a half-created stack can
  still hold billable resources.

---

## 2. Resource naming + isolation

Every trial gets a unique id and a single prefix that everything hangs off.

```
runid   = <YYYYMMDD>-<6 hex chars>          e.g. 20260807-4f9ac1
prefix  = deployeval-<task>-<model>-<runid>
        = deployeval-notes-auth-claude-opus-4-8-20260807-4f9ac1
```

Rules:

- **The prefix is the CloudFormation stack name.** One stack per trial. The
  brief given to Claude Code names this stack explicitly, so teardown and the
  free-tier audit have a single handle.
- **Every resource inside the stack derives its name/logical-id from the
  prefix** (or is left unnamed so CloudFormation auto-names it *within* the
  stack — auto-named resources are still deleted by `delete-stack`, so that is
  fine). The template should not create resources outside the stack.
- **Model alias is normalized** for name-legality: dots → hyphens
  (`claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5`), lowercase, so the
  prefix is a valid stack name, S3 bucket segment, DynamoDB table name, etc.
- **runid guarantees no collision** across reruns, across models, and across
  people cloning the repo. Two people can run the same `(task, model)` at once
  without stepping on each other because their runids differ.
- **Isolation is total:** no shared tables, buckets, or auth pools between
  trials. Because the prefix is unique and the stack is self-contained,
  `teardown.py --prefix <prefix>` removes exactly this trial and nothing else,
  and the sweep (section 5.3) can safely target the `deployeval-` family.

Keep the run manifest (`runs/<prefix>/manifest.json`) as the source of truth for
the trial: `{task, model, runid, prefix, region, started_at, ended_at,
claude_code_version}`. The model recorded here — not the transcript — is
authoritative (section 4.3).

---

## 3. Free-tier allowlist enforcement

### 3.1 The allowlist (DESIGN §3, LOCKED)

Only these services may appear in a deployed stack:

| Concern | Service | CloudFormation resource types |
|---|---|---|
| Compute | Lambda | `AWS::Lambda::Function`, `AWS::Lambda::Url`, `AWS::Lambda::Permission`, `AWS::Lambda::LayerVersion` |
| HTTP entry | Lambda Function URLs (not API Gateway) | `AWS::Lambda::Url` |
| Database | DynamoDB | `AWS::DynamoDB::Table` |
| Object storage | S3 | `AWS::S3::Bucket`, `AWS::S3::BucketPolicy` |
| Auth (optional) | Cognito **or** in-Lambda JWT | `AWS::Cognito::UserPool`, `AWS::Cognito::UserPoolClient` |
| Packaging / roles | CloudFormation + IAM for the above | `AWS::IAM::Role`, `AWS::IAM::Policy`, `AWS::CloudFormation::*`, `AWS::Logs::LogGroup` |

Explicitly **off-list** (reject if present): API Gateway (`AWS::ApiGateway::*`,
`AWS::ApiGatewayV2::*`), RDS, EC2, ELB/ALB, NAT Gateway, ECS/EKS/Fargate,
ElastiCache, OpenSearch, Kinesis, anything else. API Gateway is banned
deliberately — Function URLs cover the HTTP entry point without the API Gateway
cost surface.

The allowlist lives in one place so it can't drift: `src/allowlist.yaml`.

```yaml
# src/allowlist.yaml
allowed_resource_types:
  - AWS::Lambda::Function
  - AWS::Lambda::Url
  - AWS::Lambda::Permission
  - AWS::Lambda::LayerVersion
  - AWS::Lambda::EventInvokeConfig
  - AWS::DynamoDB::Table
  - AWS::S3::Bucket
  - AWS::S3::BucketPolicy
  - AWS::Cognito::UserPool
  - AWS::Cognito::UserPoolClient
  - AWS::Cognito::UserPoolDomain
  - AWS::IAM::Role
  - AWS::IAM::Policy
  - AWS::IAM::ManagedPolicy
  - AWS::Logs::LogGroup
  - AWS::CloudFormation::Stack        # nested-stack node itself
  - AWS::CDK::Metadata                # harmless SAM/CDK metadata
denied_resource_types:
  - AWS::ApiGateway::*
  - AWS::ApiGatewayV2::*
  - AWS::RDS::*
  - AWS::EC2::*
  - AWS::ElasticLoadBalancingV2::*
  - AWS::ECS::*
  - AWS::EKS::*
```

### 3.2 Two-sided check

Enforce on **both** the source template and the *deployed* stack, because the
two can differ (a `sam deploy` can expand `AWS::Serverless::Function` into a
Lambda + role + Function URL, and SAM transforms can add resources the author
didn't hand-write):

1. **Template check** — parse the SAM/CloudFormation template Claude Code wrote.
   SAM `AWS::Serverless::*` resources are expanded to their CloudFormation
   equivalents before checking (a `AWS::Serverless::Function` with an
   `FunctionUrlConfig` is Lambda + Function URL + role + log group — all
   allowlisted; a `AWS::Serverless::Api` is API Gateway — **denied**).
2. **Deployed-stack check** — list the *actual* resources CloudFormation
   created and check every `ResourceType` against the allowlist. This is the
   authoritative check because it sees what really got provisioned.

### 3.3 `freetier_check.py` spec

```
python3 src/freetier_check.py --stack <prefix> [--template <path>] [--region <r>]
```

Behavior:

- **Deployed-stack pass (authoritative):** call
  `cloudformation.list_stack_resources` (paginate), recursing into any nested
  `AWS::CloudFormation::Stack`. Collect every `ResourceType`. Reject any type
  not in `allowed_resource_types` or matching a `denied_resource_types` glob.
- **Template pass (optional, if `--template` given):** run the SAM transform
  locally (`sam validate` / `cfn-flip` + the SAM transform, or `sam build`'s
  packaged template) and apply the same type check to the expanded template.
- **S3 privacy assertion** (for `file-share` and any stack with a bucket): assert
  every `AWS::S3::Bucket` has `PublicAccessBlockConfiguration` fully on
  (`BlockPublicAcls`, `IgnorePublicAcls`, `BlockPublicPolicy`,
  `RestrictPublicBuckets` all `true`) and no bucket policy granting `*`
  principal read/list. A stack that is technically Lambda+S3 but leaves the
  bucket world-readable fails free-tier privacy even though the service is
  allowlisted — this backstops the storage-privacy probe.
- **Region assertion:** confirm the stack is in the expected single region;
  a resource in another region escapes teardown.
- **Output:** `{free_tier_ok: bool, offending: [{logical_id, type}], region,
  buckets_public: bool}` → `runs/<prefix>/freetier.json`. Exit non-zero if not
  ok, so CI/a wrapper can flag it, but never auto-delete on failure — deletion
  is teardown's job.

`free_tier_ok` in the results row is the AND of: only-allowlisted-types, no
public buckets, single expected region.

---

## 4. Cost metering

Two costs. Token cost is metered from the Claude Code transcript at published
Bedrock rates. AWS cost is asserted to be $0-within-free-tier — never a
fabricated dollar figure.

### 4.1 Where the Claude Code transcript lives

Claude Code writes a JSONL transcript per session under
`~/.claude/projects/<project-slug>/<session-uuid>.jsonl`, where `<project-slug>`
is the session's working directory with `/` replaced by `-`. Subagent
transcripts (if Claude Code spawned any) live under
`~/.claude/projects/<project-slug>/<session-uuid>/subagents/agent-*.jsonl`.

To capture the trial transcript in step 1.1.F: identify the session UUID for the
trial (the most recent JSONL under the project slug that matches the trial
workdir), and copy it — plus any `subagents/` files — into
`runs/<prefix>/transcript.jsonl` (and `runs/<prefix>/subagents/`). Copy
**read-only**; the meter parses a snapshot, not the live file.

> This directory is inspected read-only. Never print, copy, or commit token
> strings, credentials, account numbers, or ARNs from it. The committed sample
> transcript is scrubbed first (section 7).

### 4.2 Transcript shape (verified against Claude Code 2.1.x)

Each line is one JSON object with a top-level `type`. Types seen include
`assistant`, `user`, `system`, `file-history-snapshot`, `last-prompt`,
`ai-title`, `mode`, `permission-mode`, `agent-name`, `attachment`,
`queue-operation`, `file-history-delta`. Only `assistant` lines carry token
usage.

An `assistant` line has top-level keys `{cwd, entrypoint, gitBranch,
isSidechain, message, parentUuid, sessionId, timestamp, type, userType, uuid,
version}`. The `message` object has `{content, id, model, role, stop_reason,
stop_details, stop_sequence, type, usage}`. The `usage` object has:

```
input_tokens
output_tokens
cache_creation_input_tokens
cache_read_input_tokens
cache_creation: { ephemeral_5m_input_tokens, ephemeral_1h_input_tokens }
server_tool_use, service_tier, inference_geo, iterations, speed   # not billed here
```

### 4.3 Two metering gotchas (both verified on a real transcript)

1. **Dedup by `message.id` — streaming emits the same message many times.** In
   the current format a single assistant message appears as multiple JSONL
   lines (streaming snapshots), all sharing one `message.id`, and every
   duplicate row carries the **identical, final** full-usage object — not
   incremental deltas. On a sample session, 994 assistant lines collapsed to
   536 unique `message.id`s (297 ids repeated, every repeat byte-identical in
   usage). Summing every row would multi-count cost by ~2×. **The canonical
   DeployEval meter keeps one row per `message.id`** (last occurrence) and sums
   those. (Note: an older, pre-streaming convention summed all rows without
   dedup because partials weren't duplicated the same way; DeployEval fixes on
   dedup-by-id because that is correct for the format Claude Code emits today.
   The meter also guards: if within one `message.id` the usage objects ever
   differ, it takes the max per field and logs a warning rather than summing.)

2. **`message.model` is unreliable — take the model from the manifest.** The
   `model` field on assistant lines is stamped with the parent/session model and
   does not reliably reflect a subagent's assigned model. Token *counts* are
   true regardless; only the *rate* selection needs the real model. DeployEval
   passes the model explicitly (`--model`, from the run manifest set in step
   1.1.B) and never trusts the transcript for model identity.

### 4.4 `agent_turns` and `wall_clock_s`

- `agent_turns` = count of unique assistant `message.id`s in the trial
  transcript (after dedup).
- `wall_clock_s` = last `timestamp` − first `timestamp` across the trial's
  transcript lines. Cross-check against the manifest's `started_at`/`ended_at`.

### 4.5 Published Bedrock rates → `prices.py`

Public per-MTok rates for the three tier models, plus the standard prompt-cache
multipliers (all public information):

```python
# src/prices.py  — published per-million-token rates (public info).
# Rates are USD per 1,000,000 tokens. Cache multipliers are relative to the
# model's input rate: 5-minute cache WRITE = 1.25x input, 1-hour cache WRITE
# = 2.0x input, cache READ = 0.10x input.
PRICES = {
    # alias           input   output
    "claude-opus-4-8":  {"in": 5.00, "out": 25.00},
    "claude-sonnet-5":  {"in": 3.00, "out": 15.00},   # standard rate
    "claude-haiku-4-5": {"in": 1.00, "out":  5.00},
}

CACHE_WRITE_5M_MULT = 1.25   # ephemeral_5m_input_tokens
CACHE_WRITE_1H_MULT = 2.00   # ephemeral_1h_input_tokens
CACHE_READ_MULT     = 0.10   # cache_read_input_tokens

# Notes for reproducers:
# - These are the published list rates. Bedrock is partner-operated; confirm
#   against aws.amazon.com/bedrock/pricing for your region/date and update here.
#   The results snapshot is dated, so pin the rate you used in the row.
# - Sonnet 5 has carried an introductory rate ($2/$10) in some windows; use the
#   rate in effect on your run date and record which you used.
```

### 4.6 `cost_meter.py` spec

```
python3 src/cost_meter.py --transcript <path> --model <alias> [--out <json>]
```

Behavior:

1. Read the trial transcript (and any `subagents/*.jsonl`).
2. Keep the last row per `message.id`; guard against differing usage per id
   (take field-wise max + warn).
3. Sum, across the deduped rows:
   `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and the two
   cache-write fields (prefer the explicit
   `cache_creation.ephemeral_5m_input_tokens` /
   `ephemeral_1h_input_tokens`; fall back to the flat
   `cache_creation_input_tokens` as 5-minute write if the split is absent).
4. Cost formula (rates from `prices.py`, `r = PRICES[model]`):

   ```
   cost_usd = input_tokens        / 1e6 * r["in"]
            + output_tokens       / 1e6 * r["out"]
            + cache_write_5m       / 1e6 * r["in"] * CACHE_WRITE_5M_MULT
            + cache_write_1h       / 1e6 * r["in"] * CACHE_WRITE_1H_MULT
            + cache_read_tokens    / 1e6 * r["in"] * CACHE_READ_MULT
   ```

5. **Honesty rule (DESIGN §7):** if the transcript is truncated or the JSONL is
   malformed at the tail, mark the result `truncated: true` and label the cost a
   **floor** (`cost_is_floor: true`). Never back-fill or estimate the missing
   tail.
6. Output `runs/<prefix>/cost.json`:
   `{model, token_cost_usd, tokens: {in, out, cache_write_5m, cache_write_1h,
   cache_read}, agent_turns, wall_clock_s, rate_used, cost_is_floor}`.

### 4.7 AWS cost

Do **not** claim a dollar AWS figure. The claim is "**$0 within free-tier
limits**", supported by three facts recorded per trial: (a) only allowlisted
services were used (`free_tier_ok`), (b) everything was torn down same-run
(`teardown_verified`), and (c) sustained usage of this stack stays within
always-free / 12-month-free limits. `aws_cost_usd` is not a field; the row
carries `aws_cost_claim: "$0 within free-tier limits"` and the supporting
booleans instead. That is the founder-relevant answer and it is defensible.

---

## 5. Teardown

Teardown deletes every stack matching the prefix and verifies zero remaining
billable resources. It has a targeted mode (one trial) and a sweep mode
(belt-and-suspenders across the whole `deployeval-` family).

### 5.1 Why it must be reliable

`delete-stack` removing the single stack is what guarantees $0 (DESIGN §3, §9).
The failure mode is orphaned billable resources — most often a **non-empty S3
bucket** (CloudFormation refuses to delete a bucket with objects) or a stack
stuck in `DELETE_FAILED`. Teardown handles both explicitly.

### 5.2 `teardown.py` — targeted mode

```
python3 src/teardown.py --prefix <prefix> [--region <r>] [--verify] [--yes]
```

Steps:

1. **Resolve the stack** named `<prefix>`. If it doesn't exist, report "nothing
   to delete" and go straight to verify.
2. **Drain S3 buckets first.** For every `AWS::S3::Bucket` in the stack, empty
   it — delete all object versions and delete markers (versioned buckets) — so
   CloudFormation can delete the bucket. This is the single most common reason a
   delete hangs.
3. **`delete-stack`** and wait (`stack_delete_complete` waiter).
4. **Retry on `DELETE_FAILED`:** re-issue `delete-stack` with
   `RetainResources=[]` after draining any still-blocking bucket; if it still
   fails, list the specific stuck resources and surface them (do not silently
   give up).
5. **`--verify`** runs the verification pass (5.4).
6. **Safety:** `teardown.py` will only delete stacks whose name starts with
   `deployeval-`. It refuses any other name. Destructive; requires `--yes` in
   non-interactive use, prompts otherwise.

### 5.3 `teardown.py --sweep` — safety mode

```
python3 src/teardown.py --sweep [--region <r>] [--dry-run] [--yes]
```

- Lists **all** CloudFormation stacks whose name starts with `deployeval-` (in
  `CREATE_COMPLETE`, `UPDATE_COMPLETE`, `ROLLBACK_COMPLETE`, `DELETE_FAILED`,
  etc. — anything not already fully gone).
- Runs the targeted teardown (5.2) on each.
- `--dry-run` lists what it *would* delete and exits without deleting — always
  run `--dry-run` first.
- Same hard guard: only ever touches the `deployeval-` prefix family. It will
  not delete a stack outside that namespace, which is why isolation (section 2)
  matters. This is the "nothing bills after a run" backstop from DESIGN §9.

### 5.4 Verification pass (zero remaining billable resources)

After deletion, confirm the trial left nothing behind:

- Stack: `describe-stacks --stack-name <prefix>` returns `does not exist` (or
  status `DELETE_COMPLETE`).
- Lambda: no function whose name starts with `<prefix>`.
- DynamoDB: no table whose name starts with `<prefix>`.
- S3: no bucket whose name starts with `<prefix>`.
- Cognito: no user pool whose name starts with `<prefix>`.
- Logs: `<prefix>` log groups deleted (they are free but tidy up).

Output `runs/<prefix>/teardown.json`:
`{stack_deleted: bool, remaining: [{service, id}], teardown_verified: bool}`.
`teardown_verified` is the AND of stack-gone and empty-remaining. This boolean
feeds the results row and backs the "$0 within free-tier" claim.

---

## 6. Results schema + files layout

Aligned to DESIGN §6. One JSON row per trial; the aggregate feeds the flagship
two-panel chart (DESIGN §8).

### 6.1 Per-trial row (`results/<prefix>.json`)

```json
{
  "prefix": "deployeval-notes-auth-claude-opus-4-8-20260807-4f9ac1",
  "task": "notes-auth",
  "model": "claude-opus-4-8",
  "runid": "20260807-4f9ac1",
  "attempt": 1,
  "region": "us-west-2",
  "claude_code_version": "2.1.x",

  "shipped": true,
  "live_url_recorded": true,

  "probes_total": 8,
  "probes_passed": 7,
  "probes": [
    {"name": "liveness",              "critical": false, "passed": true},
    {"name": "happy_path",            "critical": false, "passed": true},
    {"name": "ownership_read_list",   "critical": true,  "passed": true},
    {"name": "cross_tenant_read",     "critical": true,  "passed": false},
    {"name": "cross_tenant_modify",   "critical": true,  "passed": true},
    {"name": "auth_required",         "critical": true,  "passed": true},
    {"name": "auth_spoof_rejected",   "critical": true,  "passed": true},
    {"name": "server_side_identity",  "critical": true,  "passed": true}
  ],

  "verified_working": false,
  "silent_failure": true,

  "token_cost_usd": 12.87,
  "cost_is_floor": false,
  "tokens": {"in": 41200, "out": 88010, "cache_write_5m": 620100,
             "cache_write_1h": 0, "cache_read": 1840550},
  "rate_used": {"in": 5.00, "out": 25.00},
  "agent_turns": 63,
  "wall_clock_s": 742,

  "aws_resources": [
    {"logical_id": "NotesFn",    "type": "AWS::Lambda::Function"},
    {"logical_id": "NotesFnUrl", "type": "AWS::Lambda::Url"},
    {"logical_id": "NotesTable", "type": "AWS::DynamoDB::Table"}
  ],
  "free_tier_ok": true,
  "buckets_public": false,

  "aws_cost_claim": "$0 within free-tier limits",
  "teardown_verified": true
}
```

Field notes:

- `shipped` — Claude Code produced a live URL within budget.
- `verified_working` — `shipped` AND every `critical` probe passed.
- `silent_failure` — `shipped` AND at least one `critical` probe failed. The
  headline: "said done, but leaks/breaks." (`false` if it didn't ship — a
  non-ship isn't a silent failure, it's an honest one.)
- `token_cost_usd` — section 4; `cost_is_floor` true if the transcript was
  truncated.
- `aws_resources`, `free_tier_ok`, `buckets_public` — section 3.
- `teardown_verified` — section 5.4; backs the AWS $0 claim.

Cross-model comparables (DESIGN §6): `shipped`, `verified_working`,
`silent_failure`, `token_cost_usd`.

### 6.2 Directory layout

```
tasks/<task>/brief.md          # committed: requirements, no solution
tasks/<task>/probes.py         # committed: adversarial checks vs the live URL

src/allowlist.yaml             # committed: the free-tier allowlist (one source of truth)
src/prices.py                  # committed: published per-model rates (public)
src/freetier_check.py          # committed
src/cost_meter.py              # committed
src/teardown.py                # committed
src/write_result.py            # committed

runs/<prefix>/                 # GITIGNORED — per-trial local artifacts
  manifest.json                #   task, model, runid, prefix, region, times, cc version
  prompt.txt                   #   exact prompt given to Claude Code
  workdir/                     #   the app + template Claude Code wrote
  url.txt                      #   live URL
  resources.json               #   list-stack-resources output
  transcript.jsonl             #   read-only copy of the session transcript
  subagents/                   #   read-only copies of subagent transcripts
  freetier.json                #   freetier_check.py output
  probes.json                  #   probes.py output
  cost.json                    #   cost_meter.py output
  teardown.json                #   teardown verification
  redteam/                     #   optional manual deep-dive notes (scrubbed)

results/<prefix>.json          # committed: the per-trial row (6.1)
results/summary.csv            # committed: one line per trial (the comparables)
results/AGGREGATE.json         # committed: 9-trial roll-up for the chart

examples/sample-transcript.jsonl  # committed: ONE scrubbed sample (section 7)
```

`runs/` is gitignored because raw transcripts and `resources.json` can contain
account-specific detail. Only `results/` (rows), the scripts, and a single
**scrubbed** example transcript are committed.

### 6.3 Aggregate for the chart

`results/AGGREGATE.json` rolls the 9 rows up per model for DESIGN §8:

```json
{
  "generated_at": "2026-08-07",
  "per_model": {
    "claude-opus-4-8":  {"ship_rate": 1.00, "verified_rate": 0.67,
                          "silent_failure_rate": 0.33,
                          "token_cost_usd_total": 38.6,
                          "cost_per_verified_build_usd": 19.3},
    "claude-sonnet-5":  {"ship_rate": 1.00, "verified_rate": 0.67, "...": "..."},
    "claude-haiku-4-5": {"ship_rate": 0.67, "verified_rate": 0.33, "...": "..."}
  }
}
```

- **Left panel (done vs works):** per model, `ship_rate` vs `verified_rate`; the
  gap is `silent_failure_rate`.
- **Right panel (cost vs capability):** per model, `verified_rate` vs
  `cost_per_verified_build_usd` (= total token cost ÷ verified builds).

Plus one written **transcript autopsy**: quote the transcript line where a
concrete failure happened (e.g. the cross-tenant read that skipped the ownership
check) and what it implies for model choice.

---

## 7. Reproducibility + safety

### 7.1 Teardown discipline

- Teardown (step K) runs **every trial, including non-ships** — a half-built
  stack still holds billable resources.
- Run `teardown.py --sweep --dry-run` at the **start** and **end** of a session
  to catch anything orphaned by a crashed trial. Then `--sweep --yes` to clear
  it. The sweep only ever touches the `deployeval-` prefix family.
- `teardown_verified` must be `true` in every committed results row; a `false`
  is a blocker, not a footnote — chase the remaining resource before moving on.

### 7.2 Credential hygiene

- Credentials come from **your** environment (`~/.aws/credentials`,
  `AWS_PROFILE`, or the `AWS_*` env vars). The harness reads them; it never
  writes them anywhere.
- `.gitignore` blocks `.env`, `.env.*`, `credentials*`, `aws-credentials*`,
  `*_accessKeys.csv`, `*_credentials.csv`, `*.pem`, `*.key`, and `runs/`
  (already present in the repo).
- Prefer least-privilege AWS credentials — the run needs CloudFormation, Lambda,
  DynamoDB, S3, Cognito, IAM (for the stack's roles), and CloudWatch Logs, in
  one region. It does **not** need account-wide admin. Do not run in a shared
  production account; use a sandbox account so the sweep and the free-tier audit
  can never touch unrelated resources.
- No credential, token, or session key is ever pasted into a task brief, a
  prompt, or a transcript that gets committed.

### 7.3 Confidentiality / clean-room

- The repo is clean-room and generic. No internal codenames, no AWS account
  numbers, no ARNs, no internal doc authors, no internal cost totals.
- **Committed transcripts are scrubbed.** Only one sample transcript is
  committed (`examples/sample-transcript.jsonl`), and before committing it, run
  a scrub that removes/redacts: AWS account IDs (12-digit), ARNs, access-key
  IDs, live URLs' account-specific hostnames, any bearer/token strings, and
  email addresses used in probe traffic. `runs/` itself is gitignored so raw
  transcripts never leave the machine.
- **Pre-publish grep gate.** Before publishing the snapshot, grep the whole repo
  (including `results/` and `examples/`) for banned strings: 12-digit account
  numbers, `arn:aws:`, `sk-`/token prefixes, internal codenames, and internal
  cost figures. A hit blocks publish.

### 7.4 Reproducibility

- Everything a re-runner needs is in the repo: task briefs, probes, the
  allowlist, prices, and the four scripts. "Clone and rerun" = clone, plug in
  your own AWS keys, and follow section 1.
- Each results row records `region`, `rate_used`, `claude_code_version`, and the
  dated snapshot, so a number can always be traced to the model, rate, and tool
  version that produced it.
- The results snapshot is **dated**. Published Bedrock rates and free-tier terms
  change; pin the rate used per row and note the run date, rather than assuming
  today's rate applies retroactively.
```
