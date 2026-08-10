"""Adversarial probe framework for DeployEval.

Each probe hits a LIVE deployed app and returns pass/fail. Probes are real adversarial checks
(cross-tenant reads, forged tokens, price tampering, object-privacy), not "HTTP 200". A build that
returns 200 but leaks another user's data FAILS.

Contract (see docs/PROBES.md):
  - a probe is `probe_<name>(ctx: ProbeContext) -> ProbeResult`
  - a probe NEVER raises; it catches its own errors and returns status="error"
  - fail-closed: a CRITICAL probe ending in "fail" OR "error" blocks verified_working
"""

from .core import ProbeContext, ProbeResult, ProbeStatus, run_suite

__all__ = ["ProbeContext", "ProbeResult", "ProbeStatus", "run_suite"]
