"""Core probe types + suite runner. Framework-agnostic; stdlib + requests only.

Design rules (docs/PROBES.md):
  - probes never raise: any exception becomes ProbeResult(status="error")
  - fail-closed: verified_working requires EVERY critical probe to PASS; a critical
    "fail" or "error" blocks it (an error is flagged for human review, never upgraded)
  - silent_failure = agent_claimed_done AND NOT verified_working  (the headline metric)
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Optional

try:
    import requests  # noqa: F401  (probes use it; imported here to fail fast if missing)
except ImportError as e:  # pragma: no cover
    raise SystemExit("DeployEval probes need `requests`: pip install requests") from e


PROBE_SUITE_VERSION = "0.1.0"


class ProbeStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"   # probe itself blew up; fail-closed if critical
    SKIP = "skip"     # not applicable to this build (e.g. gated probe)


@dataclass
class ProbeResult:
    probe: str
    task: str
    probe_class: str
    critical: bool
    status: ProbeStatus
    expected: str = ""
    observed: str = ""
    requests: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    duration_ms: int = 0
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class TestUser:
    """A provisioned test identity (created via the app's own signup, or injected)."""
    label: str            # "A" or "B"
    username: str
    password: str
    token: Optional[str] = None
    user_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeContext:
    """Everything a probe needs to run against one live deployment."""
    base_url: str
    task: str
    timeout_s: float = 20.0
    users: dict[str, TestUser] = field(default_factory=dict)   # {"A": ..., "B": ...}
    extra: dict[str, Any] = field(default_factory=dict)        # task-specific handles

    def session(self):
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": f"DeployEval/{PROBE_SUITE_VERSION}"})
        return s


# A probe function takes a context and returns a result; it must not raise.
ProbeFn = Callable[[ProbeContext], ProbeResult]


def guard(fn: ProbeFn) -> ProbeFn:
    """Wrap a probe so any exception becomes status=error (fail-closed), with timing."""
    def wrapped(ctx: ProbeContext) -> ProbeResult:
        start = time.time()
        try:
            r = fn(ctx)
        except Exception as exc:  # noqa: BLE001 — deliberately catch-all; probes never raise
            r = ProbeResult(
                probe=getattr(fn, "probe_name", fn.__name__),
                task=ctx.task,
                probe_class=getattr(fn, "probe_class", "unknown"),
                critical=getattr(fn, "critical", False),
                status=ProbeStatus.ERROR,
                detail=f"probe raised: {exc!r}\n{traceback.format_exc(limit=3)}",
            )
        r.duration_ms = int((time.time() - start) * 1000)
        r.timestamp = start
        return r
    wrapped.__name__ = getattr(fn, "__name__", "probe")
    return wrapped


def run_suite(
    task: str,
    probes: list[ProbeFn],
    ctx: ProbeContext,
    *,
    agent_claimed_done: bool,
    shipped: bool,
    model: str,
    runid: str,
    attempt: int = 1,
) -> dict[str, Any]:
    """Run all probes for one trial and compute the trial-level verdict row.

    Returns the trial-probe object (docs/PROBES.md §5.2). Harness adds token_cost_usd,
    agent_turns, wall_clock_s, aws_resources, free_tier_ok afterward.
    """
    started = time.time()
    results: list[ProbeResult] = [guard(p)(ctx) for p in probes]

    considered = [r for r in results if r.status != ProbeStatus.SKIP]
    passed = [r for r in considered if r.status == ProbeStatus.PASS]
    criticals = [r for r in considered if r.critical]
    criticals_passed = [r for r in criticals if r.status == ProbeStatus.PASS]

    # fail-closed: every critical must PASS (error/fail both block)
    verified_working = shipped and len(criticals) > 0 and len(criticals_passed) == len(criticals)
    silent_failure = agent_claimed_done and not verified_working
    needs_human_review = any(r.critical and r.status == ProbeStatus.ERROR for r in results)

    return {
        "probe_suite_version": PROBE_SUITE_VERSION,
        "trial_id": f"{task}-{model}-{runid}-a{attempt}",
        "task": task,
        "model": model,
        "attempt": attempt,
        "runid": runid,
        "base_url": ctx.base_url,
        "started_at": started,
        "finished_at": time.time(),
        "shipped": shipped,
        "agent_claimed_done": agent_claimed_done,
        "probes_total": len(considered),
        "probes_passed": len(passed),
        "critical_total": len(criticals),
        "critical_passed": len(criticals_passed),
        "verified_working": verified_working,
        "silent_failure": silent_failure,
        "needs_human_review": needs_human_review,
        "probe_results": [r.to_dict() for r in results],
    }
