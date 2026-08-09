"""Tests for the policy gate.

This is the file that matters most in the repo. Everything else decides what to
suggest; this decides what a machine is allowed to do to a live cluster without
asking. Each test states an operational rule a human operator would recognise,
because the gate is only trustworthy if its behaviour is obvious.
"""

from __future__ import annotations

import pytest

from kubemend.model import Action, ActionKind, Autonomy, Plan, Target
from kubemend.safety import CONSERVATIVE, STAGING, Policy, gate

PAYMENTS = Target("payments", "Deployment", "checkout")
JOBS = Target("jobs", "Deployment", "report-worker")
SYSTEM = Target("kube-system", "Deployment", "coredns")


def rollback(target=PAYMENTS, pods=2):
    return Action(
        kind=ActionKind.ROLLBACK,
        target=target,
        before={"revision": 12},
        after={"revision": 11},
        reason="rollout crashed on boot",
        impacted_pods=pods,
    )


def scale(target=PAYMENTS, pods=1):
    return Action(
        kind=ActionKind.SCALE,
        target=target,
        before={"replicas": 3},
        after={"replicas": 5},
        reason="capacity",
        impacted_pods=pods,
    )


def set_image(target=JOBS, pods=2):
    return Action(
        kind=ActionKind.SET_IMAGE,
        target=target,
        before={"image": "reg/x:v3.1.7"},
        after={"image": "reg/x:v3.1.6"},
        reason="tag does not exist",
        impacted_pods=pods,
    )


# --- refusals ----------------------------------------------------------------


def test_control_plane_namespace_is_refused():
    """The agent must never touch the machinery that would let you recover."""
    verdict = gate(Plan(actions=[rollback(SYSTEM)]), CONSERVATIVE)
    assert not verdict.allowed
    assert "protected_namespace" in verdict.blocked_by


def test_every_control_plane_namespace_is_covered():
    for ns in ("kube-system", "argocd", "flux-system", "istio-system", "cert-manager"):
        target = Target(ns, "Deployment", "anything")
        assert not gate(Plan(actions=[rollback(target)]), CONSERVATIVE).allowed


def test_action_outside_the_allowlist_is_refused():
    """Closed action set: anything not named in policy is simply unavailable."""
    verdict = gate(Plan(actions=[set_image()]), CONSERVATIVE)
    assert not verdict.allowed
    assert "action_not_allowed" in verdict.blocked_by


def test_irreversible_action_is_refused():
    """No prior state captured means no computable undo, so it never runs."""
    orphan = Action(
        kind=ActionKind.ROLLBACK, target=PAYMENTS,
        before={}, after={"revision": 11}, reason="no idea what it was",
    )
    verdict = gate(Plan(actions=[orphan]), CONSERVATIVE)
    assert not verdict.allowed
    assert "irreversible" in verdict.blocked_by


def test_mismatched_before_and_after_is_irreversible():
    """A partial 'before' would restore the wrong thing, so it counts as absent."""
    partial = Action(
        kind=ActionKind.SET_RESOURCES, target=PAYMENTS,
        before={"memory": "256Mi"}, after={"memory": "512Mi", "cpu": "2"},
        reason="oomkilled",
    )
    assert not partial.reversible
    assert not gate(Plan(actions=[partial]), CONSERVATIVE).allowed


def test_violations_carry_a_remedy():
    """A refusal has to tell the operator what to change, not just that it failed."""
    verdict = gate(Plan(actions=[set_image()]), CONSERVATIVE)
    assert all(v.remedy for v in verdict.violations)
    assert "ActionKind.SET_IMAGE" in verdict.violations[0].remedy


# --- blast radius ------------------------------------------------------------


def test_too_many_pods_is_refused():
    verdict = gate(Plan(actions=[rollback(pods=40)]), CONSERVATIVE)
    assert not verdict.allowed
    assert "blast_radius_pods" in verdict.blocked_by


def test_blast_radius_is_summed_across_the_plan():
    """Three individually harmless actions can still be a large change."""
    plan = Plan(actions=[rollback(pods=2), rollback(pods=2), rollback(pods=2)])
    assert plan.impacted_pods == 6
    assert not gate(plan, CONSERVATIVE).allowed


def test_touching_several_workloads_is_refused_by_default():
    plan = Plan(actions=[rollback(PAYMENTS, pods=1), rollback(JOBS, pods=1)])
    verdict = gate(plan, CONSERVATIVE)
    assert not verdict.allowed
    assert "blast_radius_workloads" in verdict.blocked_by


def test_crossing_namespaces_is_refused_by_default():
    policy = Policy(
        allowed_kinds=frozenset({ActionKind.ROLLBACK}),
        autonomy={ActionKind.ROLLBACK: Autonomy.APPLY},
        max_workloads=5, max_namespaces=1, max_impacted_pods=50,
    )
    plan = Plan(actions=[rollback(PAYMENTS, pods=1), rollback(JOBS, pods=1)])
    verdict = gate(plan, policy)
    assert not verdict.allowed
    assert "blast_radius_namespaces" in verdict.blocked_by


def test_a_plan_at_exactly_the_limit_is_allowed():
    """Limits are inclusive; off-by-one here means refusing legitimate fixes."""
    assert gate(Plan(actions=[rollback(pods=CONSERVATIVE.max_impacted_pods)]), CONSERVATIVE).allowed


# --- flap protection ---------------------------------------------------------


def test_repeated_remediation_stands_down():
    """A cluster needing constant fixing is a human's problem, not a loop's."""
    plan = Plan(actions=[rollback()])
    assert gate(plan, CONSERVATIVE, recent_plans=2).allowed
    stood_down = gate(plan, CONSERVATIVE, recent_plans=3)
    assert not stood_down.allowed
    assert "rate_limited" in stood_down.blocked_by


# --- autonomy ----------------------------------------------------------------


def test_rollback_may_apply_unattended_under_the_conservative_policy():
    verdict = gate(Plan(actions=[rollback()]), CONSERVATIVE)
    assert verdict.allowed
    assert verdict.autonomy is Autonomy.APPLY
    assert not verdict.violations


def test_scaling_is_held_for_review():
    verdict = gate(Plan(actions=[scale()]), CONSERVATIVE)
    assert verdict.allowed
    assert verdict.autonomy is Autonomy.PROPOSE
    assert "autonomy_ceiling" in verdict.blocked_by


def test_the_most_restricted_action_governs_the_whole_plan():
    """Half-applying a plan reasoned about as a unit is worse than applying none."""
    plan = Plan(actions=[rollback(pods=1), scale(pods=1)])
    policy = Policy(
        allowed_kinds=CONSERVATIVE.allowed_kinds,
        autonomy=CONSERVATIVE.autonomy,
        max_workloads=2, max_impacted_pods=10, max_namespaces=1,
    )
    verdict = gate(plan, policy)
    assert verdict.allowed
    assert verdict.autonomy is Autonomy.PROPOSE  # not APPLY, despite the rollback
    assert "scale" in verdict.violations[0].message


def test_unlisted_kind_falls_back_to_the_most_restrictive_level():
    policy = Policy(
        allowed_kinds=frozenset({ActionKind.RESTART}),
        autonomy={},  # nothing configured
        default_autonomy=Autonomy.REPORT,
    )
    action = Action(ActionKind.RESTART, PAYMENTS, {"restartedAt": "t0"}, {"restartedAt": "t1"}, "x", 1)
    verdict = gate(Plan(actions=[action]), policy)
    assert verdict.allowed
    assert verdict.autonomy is Autonomy.REPORT


def test_staging_is_permissive_where_conservative_is_not():
    plan = Plan(actions=[set_image()])
    assert not gate(plan, CONSERVATIVE).allowed
    assert gate(plan, STAGING).allowed


def test_staging_still_refuses_the_control_plane():
    """Loosening a policy for a lower environment must not unlock kube-system."""
    assert not gate(Plan(actions=[rollback(SYSTEM)]), STAGING).allowed


def test_staging_still_requires_reversibility():
    orphan = Action(ActionKind.ROLLBACK, PAYMENTS, {}, {"revision": 1}, "x", 1)
    assert not gate(Plan(actions=[orphan]), STAGING).allowed


# --- degenerate cases --------------------------------------------------------


def test_empty_plan_is_fine_and_does_nothing():
    """Detection concluding there is nothing to do is a good outcome, not an error."""
    verdict = gate(Plan(), CONSERVATIVE)
    assert verdict.allowed
    assert verdict.autonomy is Autonomy.REPORT
    assert not verdict.violations


def test_default_policy_permits_nothing():
    """An unconfigured policy must be inert, never accidentally permissive."""
    verdict = gate(Plan(actions=[rollback()]), Policy())
    assert not verdict.allowed
    assert "action_not_allowed" in verdict.blocked_by


def test_all_violations_are_reported_not_just_the_first():
    """An operator should see every reason at once rather than fixing them serially."""
    bad = Action(ActionKind.SET_IMAGE, SYSTEM, {}, {"image": "x"}, "x", impacted_pods=99)
    verdict = gate(Plan(actions=[bad]), CONSERVATIVE)
    assert {"protected_namespace", "action_not_allowed", "irreversible", "blast_radius_pods"} <= set(
        verdict.blocked_by
    )


def test_explain_is_readable_in_each_outcome():
    assert "permitted" in gate(Plan(actions=[rollback()]), CONSERVATIVE).explain()
    assert "held" in gate(Plan(actions=[scale()]), CONSERVATIVE).explain()
    assert "refused" in gate(Plan(actions=[rollback(SYSTEM)]), CONSERVATIVE).explain()


# --- reversibility -----------------------------------------------------------


def test_inverse_swaps_before_and_after():
    undo = rollback().inverse()
    assert undo.before == {"revision": 11}
    assert undo.after == {"revision": 12}


def test_inverse_of_inverse_is_the_original():
    original = rollback()
    assert original.inverse().inverse().after == original.after


def test_irreversible_action_refuses_to_produce_an_inverse():
    with pytest.raises(ValueError):
        Action(ActionKind.SCALE, PAYMENTS, {}, {"replicas": 3}, "x").inverse()


def test_plan_rollback_reverses_order():
    """Undo runs last-in-first-out, as any transactional rollback must."""
    plan = Plan(actions=[rollback(PAYMENTS), scale(JOBS)])
    undo = plan.rollback()
    assert [a.target for a in undo.actions] == [JOBS, PAYMENTS]


def test_a_rollback_plan_is_itself_gateable():
    """The undo path is subject to the same policy as the original change."""
    plan = Plan(actions=[rollback()])
    assert gate(plan.rollback(), CONSERVATIVE).allowed
