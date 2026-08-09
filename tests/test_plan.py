"""Tests for planning: what gets proposed, and more importantly what does not."""

from __future__ import annotations

import json
import pathlib

import pytest

from kubemend.model import ActionKind, Autonomy
from kubemend.plan import format_quantity, parse_quantity, propose, unaddressed
from kubemend.safety import CONSERVATIVE, gate
from kubemend.signals import detect

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "broken-cluster.json"


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def plans(snapshot):
    return propose(detect(snapshot), snapshot)


def plan_for(plans, name):
    for p in plans:
        if next(iter(p.targets)).name == name:
            return p
    return None


# --- quantities --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("256Mi", 256 * 1024**2), ("1Gi", 1024**3), ("512", 512), ("2G", 2 * 1000**3)],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "12Xi", None])
def test_unparseable_quantity_returns_none(bad):
    assert parse_quantity(bad) is None


def test_format_round_trips():
    assert format_quantity(parse_quantity("512Mi")) == "512Mi"
    assert format_quantity(parse_quantity("2Gi")) == "2Gi"


# --- what gets proposed ------------------------------------------------------


def test_one_plan_per_workload(plans):
    """Incidents are per service; batching them would trip every limit at once."""
    targets = [next(iter(p.targets)) for p in plans]
    assert len(targets) == len(set(targets))
    assert all(len(p.actions) == 1 for p in plans)


def test_stuck_rollout_is_answered_with_a_rollback(plans):
    plan = plan_for(plans, "checkout")
    action = plan.actions[0]
    assert action.kind is ActionKind.ROLLBACK
    assert action.before == {"revision": 12}
    assert action.after == {"revision": 11}


def test_crashloop_and_stuck_rollout_produce_a_single_action(plans):
    """Three findings describe one event; three rollbacks would be wrong."""
    plan = plan_for(plans, "checkout")
    assert len(plan.findings) >= 3
    assert len(plan.actions) == 1


def test_oomkill_doubles_the_memory_limit(plans):
    action = plan_for(plans, "api").actions[0]
    assert action.kind is ActionKind.SET_RESOURCES
    assert action.before == {"memory": "256Mi"}
    assert action.after == {"memory": "512Mi"}


def test_every_proposed_action_is_reversible(plans):
    """The gate would refuse otherwise; this asserts the planner never emits one."""
    assert all(p.reversible for p in plans)
    for p in plans:
        assert p.rollback().actions[0].after == p.actions[0].before


def test_blast_radius_reflects_the_workload_size(plans):
    """Rolling back a 3-replica Deployment disrupts 3 pods, not the 2 that alerted."""
    assert plan_for(plans, "checkout").impacted_pods == 3


# --- what is deliberately not proposed ---------------------------------------


def test_missing_configmap_gets_no_plan(plans):
    """Inventing config values is the canonical thing an agent must not do."""
    assert plan_for(plans, "emailer") is None


def test_unschedulable_pod_gets_no_plan(plans):
    """Shrinking a resource request to fit is a capacity decision, not a fix."""
    assert plan_for(plans, "ingest") is None


def test_flapping_pod_gets_no_plan(plans):
    """Restarts with no clear cause need a human to read the logs."""
    assert plan_for(plans, "frontend") is None


def test_first_revision_cannot_be_rolled_back(snapshot):
    """There is no revision 0, so no undo exists and nothing is proposed."""
    assert plan_for(propose(detect(snapshot), snapshot), "emailer") is None


def test_workload_without_a_revision_annotation_gets_no_plan():
    """Without the prior state there is no computable inverse, so we abstain."""
    snap = {
        "pods": {"items": [{
            "metadata": {"name": "x-1", "namespace": "default",
                         "ownerReferences": [{"kind": "ReplicaSet", "name": "x-abcdef123"}]},
            "spec": {"containers": [{"name": "c", "image": "i"}]},
            "status": {"phase": "Running", "containerStatuses": [{
                "name": "c", "image": "i", "ready": False, "restartCount": 3,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": ""}},
            }]},
        }]},
        "deployments": {"items": [{
            "metadata": {"name": "x", "namespace": "default"},  # no revision annotation
            "spec": {"replicas": 1}, "status": {"availableReplicas": 0, "conditions": []},
        }]},
    }
    assert propose(detect(snap), snap) == []


def test_unaddressed_reports_what_no_plan_covers(snapshot, plans):
    orphan_targets = {str(f.target) for f in unaddressed(detect(snapshot), plans)}
    assert "jobs/deployment/emailer" in orphan_targets
    assert "payments/deployment/checkout" not in orphan_targets


# --- planning and policy together --------------------------------------------


def test_per_incident_plans_pass_the_conservative_blast_radius(plans):
    """The cluster-wide plan would be refused; per-incident plans are not."""
    for plan in plans:
        verdict = gate(plan, CONSERVATIVE)
        if next(iter(plan.targets)).namespace != "kube-system":
            assert verdict.allowed, f"{plan.rationale}: {verdict.explain()}"


def test_control_plane_plan_is_proposed_then_refused(plans):
    """Detection and planning stay namespace-blind; only policy refuses.

    Keeping the refusal in one place means a new rule or planner cannot
    accidentally acquire the ability to touch kube-system.
    """
    plan = plan_for(plans, "coredns")
    assert plan is not None
    verdict = gate(plan, CONSERVATIVE)
    assert not verdict.allowed
    assert "protected_namespace" in verdict.blocked_by


def test_rollbacks_apply_and_resource_changes_wait(plans):
    assert gate(plan_for(plans, "checkout"), CONSERVATIVE).autonomy is Autonomy.APPLY
    assert gate(plan_for(plans, "api"), CONSERVATIVE).autonomy is Autonomy.PROPOSE


def test_healthy_cluster_proposes_nothing():
    assert propose([], {}) == []
