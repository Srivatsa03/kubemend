"""Tests for post-change verification.

The polling is injected, so these run in microseconds and cover the cases that
actually matter: what counts as recovery, what a failed read means, and which
outcomes justify undoing a change to production.
"""

from __future__ import annotations

import pytest

from kubemend.model import Finding, Severity, Target
from kubemend.verify import Outcome, findings_for, verify

TARGET = Target("payments", "Deployment", "checkout")


def snapshot(*, crashloop=False, unschedulable=False):
    pods = []
    if crashloop:
        pods.append({
            "metadata": {"name": "checkout-abc12345-x", "namespace": "payments",
                         "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-abc12345"}]},
            "spec": {"containers": [{"name": "checkout", "image": "i"}]},
            "status": {"phase": "Running", "containerStatuses": [{
                "name": "checkout", "image": "i", "ready": False, "restartCount": 6,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": ""}},
                "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
            }]},
        })
    if unschedulable:
        pods.append({
            "metadata": {"name": "checkout-abc12345-y", "namespace": "payments",
                         "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-abc12345"}]},
            "spec": {"containers": [{"name": "checkout", "image": "i",
                                     "resources": {"requests": {"memory": "64Gi"}}}]},
            "status": {"phase": "Pending", "containerStatuses": [], "conditions": [
                {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                 "message": "0/3 nodes are available"}]},
        })
    return {
        "pods": {"items": pods},
        "deployments": {"items": [{
            "metadata": {"name": "checkout", "namespace": "payments"},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 0 if pods else 1, "conditions": []},
        }]},
    }


def crashloop_finding():
    return Finding("crashloop", Severity.CRITICAL, TARGET, "container crashing")


def run(reads, **kw):
    """Verify against a scripted sequence of cluster reads, on a fake clock.

    Time is injected so the deadline is deterministic and the suite runs in
    microseconds; the last reading repeats so a script cannot run dry before the
    deadline does.
    """
    clock = {"t": 0.0}
    calls = list(reads)
    index = {"i": 0}

    def read():
        value = calls[min(index["i"], len(calls) - 1)]
        index["i"] += 1
        if isinstance(value, Exception):
            raise value
        return value

    def sleep(seconds):
        clock["t"] += max(seconds, 1.0)   # a no-op sleep would stall the clock

    return verify(TARGET, read, motivating=[crashloop_finding()],
                  sleep=sleep, now=lambda: clock["t"],
                  **{"timeout": 60, "interval": 10, "settle": 0, **kw})


def test_recovery_needs_two_consecutive_clean_reads():
    """A rollout looks briefly healthy as it starts; one clean read is not recovery."""
    result = run([snapshot(), snapshot()])
    assert result.outcome is Outcome.RECOVERED
    assert result.polls == 2


def test_one_clean_read_followed_by_failure_is_not_recovery():
    result = run([snapshot(), snapshot(crashloop=True)] + [snapshot(crashloop=True)])
    assert result.outcome is Outcome.STILL_FAILING


def test_a_workload_that_stays_broken_is_still_failing():
    result = run([snapshot(crashloop=True)])
    assert result.outcome is Outcome.STILL_FAILING
    assert result.remaining and result.remaining[0].rule == "crashloop"


def test_a_new_critical_problem_is_not_recovery():
    """The motivating finding cleared, but the fix broke something else."""
    result = run([snapshot(unschedulable=True)])
    assert result.outcome is Outcome.STILL_FAILING
    assert result.appeared
    assert not result.remaining


def test_unreadable_cluster_is_indeterminate_not_success():
    """Silence proves nothing; it must never pass as recovery."""
    result = run([RuntimeError("connection refused")])
    assert result.outcome is Outcome.INDETERMINATE
    assert result.errors == result.polls


def test_indeterminate_does_not_justify_a_revert():
    """Undoing a possibly-working fix on no evidence is a second outage."""
    assert not Outcome.INDETERMINATE.should_revert
    assert not Outcome.RECOVERED.should_revert
    assert Outcome.STILL_FAILING.should_revert


def test_a_read_failure_does_not_break_a_recovery_streak_into_success():
    """An error between two clean reads means we never saw it clean twice running."""
    result = run([snapshot(), RuntimeError("blip"), snapshot(), snapshot()])
    assert result.outcome is Outcome.RECOVERED
    # The streak restarted, so it took more than two polls to conclude.
    assert result.polls >= 4


def test_findings_are_scoped_to_the_workload_that_changed():
    other = Target("web", "Deployment", "frontend")
    assert findings_for(snapshot(crashloop=True), other) == []
    assert findings_for(snapshot(crashloop=True), TARGET)


def test_the_explanation_says_what_happened():
    assert "recovered" in run([snapshot(), snapshot()]).explain()
    assert "still failing" in run([snapshot(crashloop=True)]).explain()
    assert "could not read" in run([RuntimeError("x")]).explain()
