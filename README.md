# DeployEval

**Can an AI coding agent take a plain-English brief and ship a working, *secure* app on the AWS free
tier for $0, and do you need a frontier model to do it, or does a cheaper one ship the same thing?**

DeployEval hands a model a short app spec, has it design, build, and deploy the app itself, then runs
**adversarial probes against the live deployment** to measure the gap between *"the agent said it's
done"* and *"it actually works and is secure."* That gap is the headline metric: the **silent-failure
rate**.

Most coding benchmarks score whether generated code passes unit tests. DeployEval scores something
closer to what a founder actually cares about: *is the thing I deployed reachable, correct, and not
leaking my users' data, and did it cost me anything?*

---

## Results (v0.1)

Eight real AWS deployments: two Claude tiers (Opus 4.8, Sonnet 5) across four apps, each built and
deployed by the model itself, then adversarially probed and torn down. Full write-up with charts:
**[the results page](site/index.html)** (open locally, or view the published site).

| App | failure surface | Opus 4.8 | Sonnet 5 |
|---|---|---|---|
| notes-auth | auth + cross-tenant isolation | verified | verified |
| cart-pay | transaction integrity | verified | verified |
| file-share | object-storage privacy | **silent failure** | **silent failure** |
| realtime-room | realtime delivery + connect-auth | verified † | verified † |

- **The frontier model was no more correct than the mid-tier one.** Both verified 3 of 4 apps and
  produced the same one silent failure; the only thing that changed with the pricier model was the
  bill (Opus ~$11.5 vs Sonnet ~$5.3 in build tokens across the suite).
- **The flagship finding:** on `file-share`, *both* models shipped a world-readable object store. The
  presigned share URL's signature could be stripped and the bare object fetched anonymously (`HTTP
  200`). Every functional test passes; only the adversarial probe catches it.
  ([autopsy](results/_autopsy/file-share-opus-silent-failure.md))
- **† realtime-room:** verified-secure (7/7 probes) but both models pulled in a CloudFront distribution
  the brief never asked for, so it falls outside the minimal always-free service set.
  ([autopsy](results/_autopsy/realtime-opus-overprovision.md))

Two other tiers (Haiku, Fable) were excluded because they were not reliably available on the test
account. Per-trial detail is in [`results/*.json`](results/).

---

## Why this exists

An app can return `HTTP 200` and still be broken in the ways that matter: one user reading another's
private notes, a checkout that trusts a client-supplied price, an uploaded file that's world-readable,
a chat that fans messages across rooms. An agent will happily report "done" for all of these.

DeployEval attacks each deployed app the way a hostile user would, and only counts it as
**verified working** if every critical security property holds. A build that ships but fails a
critical probe is a **silent failure**, the most useful number here.

Two questions it answers per model:
1. **Does it ship, for real, for free?** A live public URL on AWS free tier, verified end to end.
2. **Do you need the expensive model?** The same benchmark across model tiers, so you can see whether
   a cheaper model ships the same working app.

---

## What it measures

Per `(task × model)` trial:

| Metric | Meaning |
|---|---|
| `shipped` | the agent produced a live, reachable public URL within budget |
| `verified_working` | shipped **and** every critical adversarial probe passed |
| `silent_failure` | the agent claimed done, but it is not verified working (**the headline**) |
| `free_tier_ok` | the deployed stack used only free-tier-allowlisted services (evidence for the $0 claim) |
| `token_cost_usd` | build token cost, metered from the session transcript (deduped; see below) |

## The four tasks (v0.1)

Each is a generic app that stresses one class of silent failure:

| Task | App | Failure surface it attacks |
|---|---|---|
| `notes-auth` | multi-user notes with login | auth + **cross-tenant isolation** |
| `cart-pay` | catalog + cart + mock checkout | **transaction integrity** (price/stock/total) |
| `file-share` | upload + share-by-link | **object-storage privacy** |
| `realtime-room` | shared room over WebSockets | **realtime delivery + connection auth** |

**38 adversarial probes** across the four (24 critical). Examples of what they actually do, not
`HTTP 200` checks:
- create two users, then have A request B's note by id -> must be `403/404`, never `200`-with-content
- inject `price_cents: 1` on an item -> the server's total must ignore it
- strip the signature off a presigned S3 URL and GET the bare object -> must be `403`
- fire two concurrent checkouts for the last unit in stock -> at most one may succeed
- present an `alg:none` / re-signed JWT at a WebSocket connect -> must be rejected at the handshake

See [`docs/PROBES.md`](docs/PROBES.md) for the full probe list and pass/fail conditions, and
[`tasks/*/brief.md`](tasks/) for the model-facing specs.

---

## Quickstart

```bash
pip install -e .            # Python 3.9+
pytest -q                   # run the unit tests (no AWS needed)

# see the run board (0/16 until you run trials)
python -m deployeval.run_all board
```

Probes are plain `requests` (plus `websocket-client` for `realtime-room`). Given a live URL you can
run one task's probes directly:

```bash
python -m deployeval.run_all measure \
  --task notes-auth --model claude-haiku-4-5 \
  --stack deployeval-notes-auth-haiku-run \
  --url https://<your-deployed-api>.execute-api.us-west-2.amazonaws.com
```

That runs the probes, audits the stack against the free-tier allowlist, writes a result row to
`results/`, and tears the stack down.

### Reproducing a full run

1. Point AWS at your own account (`AWS_PROFILE=...`). Credentials are read from your environment and
   never committed (`.env`, `credentials*` are gitignored).
2. For each `(task, model)`: run a coding agent on `tasks/<task>/brief.md`, set to the target model,
   and let it deploy (stack name `deployeval-<task>-<model>-<runid>`).
3. `measure` the resulting live URL (above). `board` shows progress and the running tally.

---

## Methodology notes (the parts that make the numbers trustworthy)

- **Adversarial, not smoke tests.** A probe passes only when the security/integrity *property* holds.
  Fail-closed: if a critical property can't be proven (contract mismatch, timeout, socket error), the
  probe is a fail/error and blocks `verified_working`. Ambiguity counts against the build.
- **Fixed API contract.** Every model builds to the same endpoint contract
  ([`docs/PROBES.md §2`](docs/PROBES.md)), so one probe file runs identically across all builds. A
  build that deviates records `error`, itself a signal it didn't meet the brief.
- **$0, verified.** Deploys use a free-tier service allowlist ([`src/deployeval/allowlist.yaml`](src/deployeval/allowlist.yaml));
  the free-tier checker audits the *actual deployed stack*, and every stack is torn down after
  measuring. (Note: public Lambda Function URLs are blocked on some accounts by SCP, so DeployEval
  uses API Gateway HTTP API as the public entry.)
- **Honest cost metering.** Token cost is summed from the session transcript **deduped by message
  id**: streaming emits one message as many rows with identical usage, so naive summing roughly
  doubles the number. Truncated transcripts are marked a floor, never back-filled with an estimate;
  unknown model prices report `null`, never a guess.

## Limitations (v0.1)

- One attempt per `(task, model)`: no consistency (pass^k) measurement yet.
- Realtime presence-cleanup and S3-privacy probes are best-effort against a fixed contract; a build
  that deviates from the contract is scored `error`, which is deliberate but coarse.
- Cost is *build* token cost; AWS cost is asserted "$0 within free-tier limits," not a dollar figure.
- The agent-run half (a model building + deploying) is driven by an operator, not fully automated in
  this repo; the measurement half is.

## License

MIT. See [LICENSE](LICENSE).
