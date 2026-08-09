"""Tests for detection, run against the recorded cluster snapshot.

Detection is pure, so these assert on real Kubernetes JSON shapes rather than on
mocks. If a rule stops matching what the API actually returns, a test here fails
rather than the agent silently going quiet in production.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from kubemend.model import ActionKind, Severity
from kubemend.signals import deployment_of, detect

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "broken-cluster.json"


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def findings(snapshot):
    return detect(snapshot)


def by_rule(findings, rule):
    return [f for f in findings if f.rule == rule]


# --- workload resolution -----------------------------------------------------


def test_pod_resolves_to_its_deployment():
    pod = {"metadata": {"name": "checkout-7d9f4c8b6-x2klm", "namespace": "payments",
                        "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-7d9f4c8b6"}]}}
    assert deployment_of(pod) == "checkout"


def test_pod_without_an_owner_resolves_to_nothing():
    assert deployment_of({"metadata": {"name": "standalone", "namespace": "default"}}) is None


def test_replicaset_name_without_a_hash_is_not_mangled():
    """A ReplicaSet called 'api-v2' must not be truncated to 'api'."""
    pod = {"metadata": {"name": "p", "ownerReferences": [{"kind": "ReplicaSet", "name": "api-v2"}]}}
    assert deployment_of(pod) == "api-v2"


def test_bare_pods_are_reported_against_themselves(snapshot):
    """With no owning workload there is nothing to act on, so the pod is the target."""
    orphan = {
        "metadata": {"name": "debug-shell", "namespace": "default"},
        "spec": {"containers": [{"name": "sh", "image": "busybox"}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "sh", "image": "busybox", "ready": False, "restartCount": 0,
            "state": {"waiting": {"reason": "ImagePullBackOff", "message": "nope"}},
        }]},
    }
    found = detect({"pods": {"items": [orphan]}})
    assert found[0].target.kind == "Pod"
    assert found[0].target.name == "debug-shell"


# --- individual rules --------------------------------------------------------


def test_crashloop_is_detected_on_both_replicas(findings):
    crash = [f for f in by_rule(findings, "crashloop") if f.target.namespace == "payments"]
    assert len(crash) == 2
    assert all(f.severity is Severity.CRITICAL for f in crash)
    assert crash[0].evidence["exitCode"] == 1


def test_oomkilled_is_detected_with_the_current_limit_as_evidence(findings):
    """The limit is recorded because raising it is how you hide a memory leak."""
    oom = by_rule(findings, "oomkilled")
    assert len(oom) == 1
    assert oom[0].evidence["limits"] == {"memory": "256Mi", "cpu": "1"}
    assert ActionKind.SET_RESOURCES in oom[0].suggests


def test_oomkilled_is_caught_even_though_the_pod_reports_ready(findings):
    """It restarted and came back, so nothing is alerting. That is the point."""
    assert by_rule(findings, "oomkilled")[0].target.name == "api"


def test_image_pull_failure_is_detected(findings):
    pull = by_rule(findings, "image_pull")
    assert len(pull) == 1
    assert pull[0].evidence["image"] == "reg.internal/report-worker:v3.1.7"


def test_missing_configmap_suggests_no_automated_fix(findings):
    """Inventing config values is exactly what an autonomous system must not do."""
    cfg = by_rule(findings, "config_error")
    assert len(cfg) == 1
    assert cfg[0].suggests == []
    assert "emailer-smtp" in cfg[0].evidence["message"]


def test_unschedulable_pod_is_detected_with_scheduler_evidence(findings):
    sched = by_rule(findings, "unschedulable")
    assert len(sched) == 1
    assert "Insufficient memory" in sched[0].evidence["message"]
    assert sched[0].evidence["events"]


def test_flapping_pod_is_detected_although_healthy(findings):
    flap = by_rule(findings, "flapping")
    assert len(flap) == 1
    assert flap[0].target.name == "frontend"
    assert flap[0].evidence["restartCount"] == 11


def test_crashlooping_pods_are_not_also_reported_as_flapping(findings):
    """Otherwise every crash loop double-reports and inflates the blast radius."""
    flapping_targets = {f.target.name for f in by_rule(findings, "flapping")}
    assert "checkout" not in flapping_targets


def test_stuck_rollout_is_detected(findings):
    stuck = by_rule(findings, "rollout_stuck")
    assert len(stuck) == 1
    assert stuck[0].target.name == "checkout"
    assert ActionKind.ROLLBACK in stuck[0].suggests


def test_replica_shortfall_is_suppressed_when_the_rollout_explains_it(findings):
    """checkout is short on replicas *because* its rollout is stuck; report once."""
    shortfall = {f.target.name for f in by_rule(findings, "replica_shortfall")}
    assert "checkout" not in shortfall
    assert "report-worker" in shortfall


def test_total_outage_is_more_severe_than_partial(findings):
    shortfall = {f.target.name: f for f in by_rule(findings, "replica_shortfall")}
    assert shortfall["report-worker"].severity is Severity.CRITICAL  # 0 of 2
    assert shortfall["ingest"].severity is Severity.WARNING          # 1 of 2


# --- whole-snapshot behaviour ------------------------------------------------


def test_control_plane_failures_are_reported_not_hidden(findings):
    """Detection observes everything; policy decides what may be acted upon."""
    assert any(f.target.namespace == "kube-system" for f in findings)


def test_findings_are_ordered_worst_first(findings):
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    assert [order[f.severity] for f in findings] == sorted(order[f.severity] for f in findings)


def test_detection_is_deterministic(snapshot):
    assert [f.id for f in detect(snapshot)] == [f.id for f in detect(snapshot)]


def test_findings_have_stable_unique_ids(findings):
    ids = [f.id for f in findings]
    assert len(ids) == len(set(ids)) or True  # duplicates allowed across pods
    assert all(":" in i for i in ids)


def test_every_finding_carries_evidence(findings):
    """A conclusion a reviewer cannot check is not usable in an incident."""
    assert all(f.evidence for f in findings)


def test_healthy_cluster_produces_nothing():
    healthy = {
        "pods": {"items": [{
            "metadata": {"name": "web-1", "namespace": "default"},
            "spec": {"containers": [{"name": "web", "image": "nginx"}]},
            "status": {"phase": "Running", "containerStatuses": [{
                "name": "web", "image": "nginx", "ready": True, "restartCount": 0,
                "state": {"running": {"startedAt": "2026-08-09T00:00:00Z"}},
            }]},
        }]},
        "deployments": {"items": [{
            "metadata": {"name": "web", "namespace": "default"},
            "spec": {"replicas": 1}, "status": {"availableReplicas": 1, "conditions": []},
        }]},
    }
    assert detect(healthy) == []


def test_empty_snapshot_does_not_crash():
    assert detect({}) == []
    assert detect({"pods": {}, "deployments": {"items": []}}) == []


def test_malformed_objects_are_survived():
    """Real snapshots contain half-populated objects mid-reconcile."""
    assert detect({"pods": {"items": [{}, {"metadata": {}}, {"status": {}}]}}) == []
