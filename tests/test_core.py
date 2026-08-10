"""Unit tests for DeployEval core invariants. No AWS or network needed.

Run: pytest -q   (from repo root, with src on the path via `pip install -e .`)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deployeval.probes.core import (  # noqa: E402
    ProbeContext, ProbeResult, ProbeStatus, run_suite,
)
from deployeval.cost_meter import sum_tokens, cost_usd, TokenTotals  # noqa: E402
from deployeval.freetier_check import allowed_types  # noqa: E402


# ---- probe suite scoring / fail-closed -------------------------------------

def _probe(name, status, critical):
    def fn(ctx):
        return ProbeResult(probe=name, task="t", probe_class="c", critical=critical, status=status)
    return fn


def _run(probes, claimed_done=True, shipped=True):
    ctx = ProbeContext(base_url="http://x", task="t")
    return run_suite("t", probes, ctx, agent_claimed_done=claimed_done, shipped=shipped,
                     model="claude-haiku-4-5", runid="test")


def test_all_critical_pass_is_verified():
    row = _run([_probe("a", ProbeStatus.PASS, True), _probe("b", ProbeStatus.PASS, True)])
    assert row["verified_working"] is True
    assert row["silent_failure"] is False


def test_critical_fail_blocks_verified_and_flags_silent_failure():
    row = _run([_probe("a", ProbeStatus.PASS, True), _probe("b", ProbeStatus.FAIL, True)])
    assert row["verified_working"] is False
    assert row["silent_failure"] is True  # claimed done but not verified


def test_critical_error_is_fail_closed():
    # an error on a critical probe must NOT be upgraded to pass
    row = _run([_probe("a", ProbeStatus.ERROR, True)])
    assert row["verified_working"] is False
    assert row["needs_human_review"] is True


def test_noncritical_fail_does_not_block():
    row = _run([_probe("a", ProbeStatus.PASS, True), _probe("b", ProbeStatus.FAIL, False)])
    assert row["verified_working"] is True


def test_not_shipped_is_not_verified():
    row = _run([_probe("a", ProbeStatus.PASS, True)], shipped=False)
    assert row["verified_working"] is False


def test_skip_not_counted():
    row = _run([_probe("a", ProbeStatus.PASS, True), _probe("b", ProbeStatus.SKIP, True)])
    # only the passing critical counts; skip excluded
    assert row["critical_total"] == 1
    assert row["verified_working"] is True


# ---- cost meter dedup ------------------------------------------------------

def test_cost_meter_dedupes_by_message_id(tmp_path):
    # two rows share message id -> counted once; a third distinct id counted too
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text(
        '{"message":{"id":"m1","usage":{"input_tokens":100,"output_tokens":10}}}\n'
        '{"message":{"id":"m1","usage":{"input_tokens":100,"output_tokens":10}}}\n'  # duplicate
        '{"message":{"id":"m2","usage":{"input_tokens":50,"output_tokens":5}}}\n'
    )
    t = sum_tokens(jsonl)
    assert t.messages_counted == 2
    assert t.duplicate_rows_skipped == 1
    assert t.input_tokens == 150   # 100 + 50, NOT 250
    assert t.output_tokens == 15


def test_cost_unknown_model_returns_none():
    assert cost_usd(TokenTotals(input_tokens=1000), "no-such-model") is None


def test_cost_known_model_computes():
    t = TokenTotals(input_tokens=1_000_000, output_tokens=1_000_000)
    # haiku 4-5: $1 in / $5 out per M
    assert cost_usd(t, "claude-haiku-4-5") == 6.0


# ---- allowlist -------------------------------------------------------------

def test_function_urls_banned_globally():
    allow = allowed_types("notes-auth")
    assert "AWS::Lambda::Url" not in allow          # banned (SCP 403s on this account)
    assert "AWS::ApiGatewayV2::Api" in allow        # the required public entry
    assert "AWS::DynamoDB::Table" in allow
