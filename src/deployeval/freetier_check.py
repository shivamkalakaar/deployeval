"""Free-tier allowlist audit for a deployed DeployEval stack.

Given a deployed CloudFormation stack, list its actual resources and assert every resource type is
allowlisted (global allow + the task-specific carve-out, e.g. WebSocket APIGW for realtime-room).
Also asserts S3 buckets have public access blocked. Returns free_tier_ok + the offending list.

This backs the "$0 within free-tier" claim with evidence, rather than trusting the agent's word.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .awsenv import resolve_session

ALLOWLIST = Path(__file__).with_name("allowlist.yaml")


def _load_allowlist() -> dict:
    return yaml.safe_load(ALLOWLIST.read_text())


def allowed_types(task: str) -> set[str]:
    al = _load_allowlist()
    allowed = set(al.get("global_allow", []))
    allowed |= set((al.get("task_allow", {}) or {}).get(task, []))
    return allowed


def check_stack(stack_name: str, task: str, sess=None) -> dict:
    """Audit a deployed stack. Returns a dict with free_tier_ok + details."""
    sess = sess or resolve_session()
    cf = sess.client("cloudformation")
    allow = allowed_types(task)
    banned = set(_load_allowlist().get("banned", []))

    resources = []
    paginator = cf.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=stack_name):
        resources.extend(page["StackResourceSummaries"])

    # A prefix entry ending in "::" (e.g. "Custom::") matches any type starting with it —
    # CloudFormation custom resources surface as "Custom::<Name>", all deploy-time-only helpers.
    prefixes = {a for a in allow if a.endswith("::")}

    def _is_allowed(t: str) -> bool:
        return t in allow or any(t.startswith(p) for p in prefixes)

    types = sorted({r["ResourceType"] for r in resources})
    off_allowlist = [t for t in types if not _is_allowed(t)]
    banned_hit = [t for t in types if t in banned]

    # S3 public-access-block assertion
    s3 = sess.client("s3")
    s3_findings = []
    for r in resources:
        if r["ResourceType"] == "AWS::S3::Bucket" and r.get("PhysicalResourceId"):
            b = r["PhysicalResourceId"]
            try:
                pab = s3.get_public_access_block(Bucket=b)["PublicAccessBlockConfiguration"]
                blocked = all(pab.get(k) for k in
                              ("BlockPublicAcls", "IgnorePublicAcls",
                               "BlockPublicPolicy", "RestrictPublicBuckets"))
                if not blocked:
                    s3_findings.append({"bucket": b, "public_access_block": pab})
            except Exception as exc:  # noqa: BLE001 — missing PAB == not blocked
                s3_findings.append({"bucket": b, "error": f"no public-access-block ({exc!r})"})

    free_tier_ok = not off_allowlist and not banned_hit and not s3_findings
    return {
        "stack": stack_name,
        "task": task,
        "resource_types": types,
        "off_allowlist": off_allowlist,
        "banned_hit": banned_hit,
        "s3_public_findings": s3_findings,
        "free_tier_ok": free_tier_ok,
    }
