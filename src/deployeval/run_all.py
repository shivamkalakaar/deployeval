"""DeployEval run orchestrator — sequential, supervised.

Division of labor (honest):
  - BUILD half (a model-agent designs+deploys its stack) is driven by the operator (Claude Code
    spawns one subagent per trial). A plain Python script cannot spawn those agents, so this module
    does NOT fire them; it tracks and MEASURES them.
  - MEASURE half runs here, in the operator's own shell (rock-solid even if a build agent dies after
    deploying): free-tier audit -> probes -> cost -> teardown -> write result row -> update board.

Commands:
  python -m deployeval.run_all board                          # show the progress board
  python -m deployeval.run_all measure --task T --model M \\
        --stack S --url U [--claimed-done] [--transcript path] [--no-teardown]
  python -m deployeval.run_all next                           # print the next pending trial to build
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "results"
RESULTS.mkdir(exist_ok=True)

# Model matrix: four tiers fully available on this Bedrock account, newest to prior generation.
# Haiku and Fable are excluded: neither is reliably enabled here (Haiku silently falls back to Opus;
# Fable returns 403 "not available" intermittently), so neither can be a distinct, labeled trial.
# Matrix is 4 model tiers x 4 tasks = 16 trials.
TASKS = ["notes-auth", "cart-pay", "file-share", "realtime-room"]
MODELS = ["claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6"]
TRIALS = [(t, m) for t in TASKS for m in MODELS]  # 16 (4 models x 4 tasks), task-major

STATE_ICON = {
    "pending": "· ",
    "verified": "✓ ",
    "silent_failure": "✗ ",
    "not_shipped": "○ ",
    "error": "! ",
}


def _result_path(task: str, model: str) -> Path:
    return RESULTS / f"{task}__{model}.json"


def load_row(task: str, model: str):
    p = _result_path(task, model)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _state(row) -> str:
    if row is None:
        return "pending"
    if not row.get("shipped"):
        return "not_shipped"
    if row.get("verified_working"):
        return "verified"
    if row.get("silent_failure"):
        return "silent_failure"
    return "error"


def board() -> int:
    """Print the progress board with a bar + tallies."""
    rows = {(t, m): load_row(t, m) for (t, m) in TRIALS}
    done = sum(1 for r in rows.values() if r is not None)
    tally = {"verified": 0, "silent_failure": 0, "not_shipped": 0, "error": 0}
    for r in rows.values():
        if r is not None:
            tally[_state(r)] += 1

    width = 28
    filled = int(width * done / len(TRIALS))
    bar = "█" * filled + "░" * (width - filled)

    print(f"\nDeployEval — {done}/{len(TRIALS)} trials measured")
    print(f"[{bar}] {int(100*done/len(TRIALS))}%")
    print(f"  ✓ verified {tally['verified']}   ✗ silent-failure {tally['silent_failure']}   "
          f"○ not-shipped {tally['not_shipped']}   ! error {tally['error']}\n")

    # grid: rows = tasks, cols = models
    colw = 18
    header = " " * 16 + "".join(m.replace("claude-", "")[:colw].ljust(colw) for m in MODELS)
    print(header)
    for t in TASKS:
        line = t.ljust(16)
        for m in MODELS:
            st = _state(rows[(t, m)])
            cell = STATE_ICON[st] + (st if st != "pending" else "")
            line += cell.ljust(colw)
        print(line)
    print()
    return 0


def next_trial() -> int:
    for (t, m) in TRIALS:
        if load_row(t, m) is None:
            print(f"NEXT: task={t} model={m}")
            print(f"  stack name to use: deployeval-{t}-{m.replace('claude-','').replace('.','-')}-run")
            return 0
    print(f"All {len(TRIALS)} trials measured. Run `board` to see results.")
    return 0


def measure(args) -> int:
    """Measure one deployed trial end to end and write its result row."""
    from .probes.core import ProbeContext, run_suite
    from .freetier_check import check_stack
    from .cost_meter import meter as cost_meter
    from . import teardown as td
    from .awsenv import resolve_session

    task, model, stack, url = args.task, args.model, args.stack, args.url
    if task not in TASKS:
        print(f"unknown task {task}; expected one of {TASKS}"); return 2

    # import the right probe module
    probe_mod = {
        "notes-auth": "deployeval.probes.notes_auth",
        "cart-pay": "deployeval.probes.cart_pay",
        "file-share": "deployeval.probes.file_share",
        "realtime-room": "deployeval.probes.realtime_room",
    }.get(task)
    if probe_mod is None:
        print(f"probes for task '{task}' not implemented yet."); return 2
    import importlib
    PROBES = importlib.import_module(probe_mod).PROBES

    print(f"[measure] {task} × {model}  stack={stack}")

    # 1) free-tier audit
    sess = resolve_session()
    ft = check_stack(stack, task, sess=sess)
    print(f"  free_tier_ok={ft['free_tier_ok']}  off_allowlist={ft['off_allowlist']}  banned={ft['banned_hit']}")

    # 2) probes against the live URL. For realtime-room, auth (signup/login) is HTTP and may live on
    #    a SEPARATE host from the wss endpoint (the correct free-tier architecture); pass it explicitly.
    ctx = ProbeContext(base_url=url, task=task, timeout_s=20)
    if getattr(args, "auth_url", None):
        ctx.extra["auth_base"] = args.auth_url.rstrip("/")
    row = run_suite(task, PROBES, ctx, agent_claimed_done=args.claimed_done,
                    shipped=True, model=model, runid=args.runid or "run")

    # 3) cost. Prefer the authoritative per-build cost the headless CLI reports straight from
    #    Bedrock (--cost-usd); fall back to transcript dedup metering; else unknown.
    if args.cost_usd is not None:
        row["cost"] = {"token_cost_usd": round(args.cost_usd, 6), "cost_known": True,
                       "source": "bedrock-cli-total_cost_usd"}
    elif args.transcript:
        row["cost"] = cost_meter(args.transcript, model)
    else:
        row["cost"] = {"token_cost_usd": None, "cost_known": False, "note": "no cost source supplied"}

    # 4) attach free-tier + metadata
    row["free_tier_ok"] = ft["free_tier_ok"]
    row["free_tier_detail"] = ft
    row["stack"] = stack
    row["measured_at"] = datetime.now(timezone.utc).isoformat()

    _result_path(task, model).write_text(json.dumps(row, indent=2))
    print(f"  -> {_state(row).upper()}  ({row['probes_passed']}/{row['probes_total']} probes, "
          f"{row['critical_passed']}/{row['critical_total']} critical)")
    print(f"  saved results/{task}__{model}.json")

    # 5) teardown unless told not to
    if not args.no_teardown:
        print("  tearing down...")
        try:
            td.delete_stack(sess, stack)
        except SystemExit as e:
            print(f"  teardown refused: {e}")
    else:
        print("  (left stack up; --no-teardown)")

    board()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DeployEval orchestrator (sequential, supervised).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("board")
    sub.add_parser("next")
    mp = sub.add_parser("measure")
    mp.add_argument("--task", required=True)
    mp.add_argument("--model", required=True)
    mp.add_argument("--stack", required=True)
    mp.add_argument("--url", required=True)
    mp.add_argument("--auth-url", default=None,
                    help="realtime-room only: separate HTTP host serving /auth/signup+/auth/login "
                         "when auth is not on the wss host")
    mp.add_argument("--runid", default="run")
    mp.add_argument("--claimed-done", action="store_true", default=True)
    mp.add_argument("--not-claimed-done", dest="claimed_done", action="store_false")
    mp.add_argument("--transcript", default=None)
    mp.add_argument("--cost-usd", type=float, default=None,
                    help="authoritative build cost from the headless CLI's total_cost_usd")
    mp.add_argument("--no-teardown", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "board":
        return board()
    if args.cmd == "next":
        return next_trial()
    if args.cmd == "measure":
        return measure(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
