"""Tests for earned autonomy.

The property under test is not "the arithmetic is right", it is that the
asymmetries hold: demotion is cheap, promotion is expensive, and neither can
touch a hard refusal. Those are the parts that would be dangerous to get wrong,
so they are the parts asserted most.
"""

from __future__ import annotations

import pytest

from kubemend.earn import Record, adjust
from kubemend.journal import Journal
from kubemend.model import Action, ActionKind, Autonomy, Finding, Plan, Severity, Target
from kubemend.safety import CONSERVATIVE, Policy, gate

PAY = Target("payments", "Deployment", "checkout")
SYS = Target("kube-system", "Deployment", "coredns")


def clean(n: int) -> Record:
    return Record("payments/checkout", committed=n, verified=n, streak=n)


def burned(committed: int = 6, reverted: int = 1) -> Record:
    """A workload whose most recent fix was withdrawn: streak is zero."""
    return Record("payments/checkout", committed=committed,
                  verified=committed - reverted, reverted=reverted, streak=0)


def plan_for(target=PAY, kind=ActionKind.ROLLBACK):
    return Plan(
        findings=[Finding("crashloop", Severity.CRITICAL, target, "crashing")],
        actions=[Action(kind, target, {"revision": 2}, {"revision": 1}, "bad release",
                        impacted_pods=2)],
        rationale="test",
    )


# --- the asymmetry ------------------------------------------------------------


def test_one_withdrawn_fix_demotes_immediately():
    """No threshold on the way down. Being recently wrong is enough."""
    moved = adjust(Autonomy.APPLY, burned(committed=99, reverted=1),
                   ceiling=Autonomy.APPLY)

    assert moved.level is Autonomy.PROPOSE
    assert moved.demoted
    assert "did not hold" in moved.reason


def test_promotion_needs_a_long_clean_run():
    for streak in (1, 5, 9):
        moved = adjust(Autonomy.PROPOSE, clean(streak), ceiling=Autonomy.APPLY)
        assert moved.level is Autonomy.PROPOSE, f"promoted too early at {streak}"
        assert not moved.promoted

    moved = adjust(Autonomy.PROPOSE, clean(10), ceiling=Autonomy.APPLY)
    assert moved.level is Autonomy.APPLY
    assert moved.promoted


def test_a_long_record_does_not_outweigh_a_recent_revert():
    """99 good fixes and one bad one yesterday is not a good record today."""
    moved = adjust(Autonomy.APPLY, burned(committed=100, reverted=1),
                   ceiling=Autonomy.APPLY)
    assert moved.demoted


# --- what evidence may never do -----------------------------------------------


def test_promotion_cannot_pass_the_policy_ceiling():
    moved = adjust(Autonomy.PROPOSE, clean(500), ceiling=Autonomy.PROPOSE)

    assert moved.level is Autonomy.PROPOSE
    assert not moved.promoted
    assert "ceiling policy allows" in moved.reason


def test_earned_autonomy_is_off_unless_policy_opens_headroom():
    """Default ceiling is the starting level, so a policy opts in or gets nothing."""
    moved = adjust(Autonomy.PROPOSE, clean(500))
    assert moved.level is Autonomy.PROPOSE
    assert not moved.promoted


def test_a_spotless_record_cannot_unlock_a_protected_namespace():
    """The strongest property here: good behaviour buys a shorter leash, not a
    different rulebook."""
    policy = Policy(
        allowed_kinds=frozenset({ActionKind.ROLLBACK}),
        autonomy={ActionKind.ROLLBACK: Autonomy.APPLY},
        earned_ceiling={ActionKind.ROLLBACK: Autonomy.APPLY},
    )
    verdict = gate(plan_for(SYS), policy, record=clean(500))

    assert not verdict.allowed
    assert [v.code for v in verdict.violations] == ["protected_namespace"]


def test_evidence_does_not_rescue_a_blast_radius_refusal():
    policy = Policy(
        allowed_kinds=frozenset({ActionKind.SCALE}),
        autonomy={ActionKind.SCALE: Autonomy.APPLY},
        max_impacted_pods=2,
    )
    plan = Plan(
        findings=[Finding("replica_shortfall", Severity.CRITICAL, PAY, "short")],
        actions=[Action(ActionKind.SCALE, PAY, {"replicas": 2}, {"replicas": 40},
                        "capacity", impacted_pods=40)],
        rationale="test",
    )
    verdict = gate(plan, policy, record=clean(500))

    assert not verdict.allowed
    assert "blast_radius_pods" in [v.code for v in verdict.violations]


# --- cold start ---------------------------------------------------------------


def test_no_history_changes_nothing():
    moved = adjust(Autonomy.PROPOSE, Record("payments/checkout"), ceiling=Autonomy.APPLY)

    assert moved.level is Autonomy.PROPOSE
    assert not moved.changed
    assert "no history" in moved.reason


def test_a_thin_record_is_not_evidence():
    """Two good outcomes is luck, not a track record."""
    moved = adjust(Autonomy.PROPOSE, clean(2), ceiling=Autonomy.APPLY)

    assert not moved.changed
    assert "needs 5" in moved.reason


def test_the_gate_without_a_record_behaves_exactly_as_before():
    with_none = gate(plan_for(), CONSERVATIVE)
    assert with_none.allowed
    assert with_none.autonomy is Autonomy.APPLY
    assert "earned_autonomy" not in [v.code for v in with_none.violations]


# --- it explains itself -------------------------------------------------------


def test_every_adjustment_carries_its_reason():
    for record in (clean(0), clean(2), clean(10), burned()):
        moved = adjust(Autonomy.PROPOSE, record, ceiling=Autonomy.APPLY)
        assert moved.reason, "an autonomy level that moved silently is the bug"


def test_the_gate_reports_why_the_level_moved():
    policy = Policy(
        allowed_kinds=frozenset({ActionKind.ROLLBACK}),
        autonomy={ActionKind.ROLLBACK: Autonomy.APPLY},
        earned_ceiling={ActionKind.ROLLBACK: Autonomy.APPLY},
    )
    verdict = gate(plan_for(), policy, record=burned())

    assert verdict.allowed
    assert verdict.autonomy is Autonomy.PROPOSE
    earned = [v for v in verdict.violations if v.code.startswith("earned_")]
    assert earned and "did not hold" in earned[0].message
    assert "kubemend log" in earned[0].remedy


# --- the record comes from real history ---------------------------------------


class FakeEmission:
    def __init__(self, commit="", branch="main"):
        self.commit, self.branch, self.pr_url, self.changes = commit, branch, "", []


def record_incident(journal, run, *, commit="", verification="", reverted=""):
    from kubemend.verify import Outcome, Verification
    plan = plan_for()
    verdict = gate(plan, CONSERVATIVE)
    result = None
    if verification:
        result = Verification(target=PAY, outcome=Outcome(verification), waited=30.0)
    journal.record(run, plan, verdict, FakeEmission(commit), result, reverted)


@pytest.fixture()
def journal(tmp_path):
    j = Journal(tmp_path / "j.db")
    yield j
    j.close()


def test_the_streak_counts_back_to_the_last_revert(journal):
    run = journal.start_run("conservative", "", "test")
    record_incident(journal, run, commit="a1", verification="recovered")
    record_incident(journal, run, commit="a2", verification="recovered")
    record_incident(journal, run, commit="a3", verification="still_failing", reverted="r1")
    record_incident(journal, run, commit="a4", verification="recovered")
    record_incident(journal, run, commit="a5", verification="recovered")

    rec = journal.record_for("payments", "checkout")

    assert rec.committed == 5
    assert rec.reverted == 1
    assert rec.streak == 2, "the streak must stop at the revert, not span it"


def test_an_unverified_commit_stops_the_streak_without_counting_against(journal):
    run = journal.start_run("conservative", "", "test")
    record_incident(journal, run, commit="b1", verification="recovered")
    record_incident(journal, run, commit="b2")               # committed, never verified
    record_incident(journal, run, commit="b3", verification="recovered")

    rec = journal.record_for("payments", "checkout")

    assert rec.reverted == 0          # not a failure
    assert rec.streak == 1            # but not evidence either


def test_refusals_are_not_part_of_the_record(journal):
    run = journal.start_run("conservative", "", "test")
    for _ in range(3):
        record_incident(journal, run)                        # no commit at all

    rec = journal.record_for("payments", "checkout")

    assert rec.empty
    assert rec.committed == 0


def test_a_revert_never_silences_the_agent():
    """Demotion floors at propose. A workload whose fix just failed is probably
    still broken, and REPORT would mean going quiet exactly when a human wants
    to see the proposal."""
    for base in (Autonomy.APPLY, Autonomy.PROPOSE):
        moved = adjust(base, burned(), ceiling=Autonomy.APPLY)
        assert moved.level is Autonomy.PROPOSE, f"{base.value} demoted past propose"

    # Policy sitting at report is policy's decision, not something to override.
    moved = adjust(Autonomy.REPORT, burned(), ceiling=Autonomy.REPORT)
    assert moved.level is Autonomy.REPORT
