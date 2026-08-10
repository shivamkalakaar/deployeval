"""Teardown for DeployEval — delete every stack we created, verify $0 aftermath.

Safety design:
  - HARD-GUARDED to the 'deployeval-' name prefix. Refuses to touch anything else.
  - Drains S3 buckets before delete-stack (non-empty buckets are the #1 delete blocker).
  - --dry-run lists what WOULD be deleted and touches nothing.
  - Targeted mode: teardown one stack by name. Sweep mode: all deployeval-* stacks.
  - Verifies deletion and reports any stragglers.

Usage:
  python -m deployeval.teardown --stack deployeval-notes-auth-opus-<runid>
  python -m deployeval.teardown --sweep --dry-run
  python -m deployeval.teardown --sweep            # deletes ALL deployeval-* stacks (asks confirm)
"""

from __future__ import annotations

import argparse
import sys
import time

from .awsenv import resolve_session

PREFIX = "deployeval-"
LIVE_STATES = [
    "CREATE_COMPLETE", "CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED",
    "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED",
    "DELETE_FAILED", "CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS",
]


def _guard(name: str) -> None:
    if not name.startswith(PREFIX):
        raise SystemExit(f"REFUSING: '{name}' does not start with '{PREFIX}'. Teardown is "
                         f"prefix-guarded and will not touch non-DeployEval stacks.")


def list_stacks(sess) -> list[str]:
    cf = sess.client("cloudformation")
    names: list[str] = []
    paginator = cf.get_paginator("list_stacks")
    for page in paginator.paginate(StackStatusFilter=LIVE_STATES):
        for s in page["StackSummaries"]:
            if s["StackName"].startswith(PREFIX):
                names.append(s["StackName"])
    return sorted(set(names))


def _drain_stack_buckets(sess, stack: str) -> None:
    """Empty any S3 buckets owned by this stack so delete-stack won't fail on non-empty buckets."""
    cf = sess.client("cloudformation")
    s3 = sess.resource("s3")
    try:
        resources = cf.list_stack_resources(StackName=stack)["StackResourceSummaries"]
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not list resources for {stack}: {exc!r}")
        return
    for r in resources:
        if r["ResourceType"] == "AWS::S3::Bucket" and r.get("PhysicalResourceId"):
            bucket = r["PhysicalResourceId"]
            try:
                b = s3.Bucket(bucket)
                b.object_versions.delete()  # handles versioned + unversioned
                print(f"  - drained S3 bucket {bucket}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not drain bucket {bucket}: {exc!r}")


def delete_stack(sess, stack: str, wait: bool = True) -> bool:
    _guard(stack)
    cf = sess.client("cloudformation")
    _drain_stack_buckets(sess, stack)
    print(f"  deleting stack {stack} ...")
    cf.delete_stack(StackName=stack)
    if not wait:
        return True
    waiter = cf.get_waiter("stack_delete_complete")
    try:
        waiter.wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        print(f"  ✓ deleted {stack}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ delete did not complete for {stack}: {exc!r}")
        return False


def verify_gone(sess, stacks: list[str]) -> list[str]:
    remaining = [s for s in list_stacks(sess) if s in stacks]
    return remaining


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeployEval teardown (prefix-guarded).")
    ap.add_argument("--stack", help="delete one stack by exact name")
    ap.add_argument("--sweep", action="store_true", help="delete ALL deployeval-* stacks")
    ap.add_argument("--dry-run", action="store_true", help="list only; delete nothing")
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = ap.parse_args(argv)

    sess = resolve_session()

    if args.stack:
        _guard(args.stack)
        targets = [args.stack]
    elif args.sweep:
        targets = list_stacks(sess)
    else:
        ap.error("specify --stack <name> or --sweep")

    if not targets:
        print("No deployeval-* stacks found. Nothing to tear down (account is clean).")
        return 0

    print(f"Targets ({len(targets)}):")
    for t in targets:
        print("  -", t)

    if args.dry_run:
        print("\n[dry-run] Nothing deleted.")
        return 0

    if not args.yes:
        resp = input(f"\nDelete these {len(targets)} stack(s)? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return 1

    ok = True
    for t in targets:
        ok = delete_stack(sess, t) and ok
        time.sleep(1)

    remaining = verify_gone(sess, targets)
    if remaining:
        print(f"\n⚠ {len(remaining)} stack(s) still present: {remaining}")
        return 2
    print("\n✓ All targeted stacks deleted. Account clean of deployeval-* stacks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
