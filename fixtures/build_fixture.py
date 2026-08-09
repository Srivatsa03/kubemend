"""Generate a recorded cluster snapshot with a realistic mix of failures.

Writing this as a builder rather than by hand keeps the Kubernetes shapes exact
(the nesting under status.containerStatuses is easy to get subtly wrong, and a
fixture that does not match what kubectl really emits tests nothing).

The scenario is one bad afternoon in a small cluster:

  payments/checkout   a rollout went out and the container crashes on boot
  payments/api        memory limit too low; the kernel is killing it
  jobs/report-worker  image tag does not exist in the registry
  jobs/emailer        references a ConfigMap nobody created
  analytics/ingest    asks for more memory than any node has
  web/frontend        restarting quietly while reporting healthy
  kube-system/coredns broken too, and off limits regardless

The control-plane failure is there on purpose: any remediation the agent
proposes for it must be refused by policy, not by luck.
"""

from __future__ import annotations

import json
import pathlib


def container_status(name, image, *, ready=True, restarts=0, waiting=None, last_terminated=None):
    cs = {"name": name, "image": image, "ready": ready, "restartCount": restarts, "state": {}}
    if waiting:
        cs["state"]["waiting"] = waiting
    else:
        cs["state"]["running"] = {"startedAt": "2026-08-09T14:02:11Z"}
    if last_terminated:
        cs["lastState"] = {"terminated": last_terminated}
    return cs


def pod(name, namespace, *, rs=None, phase="Running", statuses=(), containers=(), conditions=()):
    meta = {"name": name, "namespace": namespace}
    if rs:
        meta["ownerReferences"] = [
            {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": rs, "controller": True}
        ]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": meta,
        "spec": {"containers": list(containers)},
        "status": {
            "phase": phase,
            "containerStatuses": list(statuses),
            "conditions": list(conditions),
        },
    }


def deployment(name, namespace, *, replicas, available, conditions=(), revision=None):
    meta = {"name": name, "namespace": namespace}
    if revision is not None:
        # Kubernetes records the current rollout revision here; it is what
        # `kubectl rollout undo` reads to find the previous ReplicaSet.
        meta["annotations"] = {"deployment.kubernetes.io/revision": str(revision)}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta,
        "spec": {"replicas": replicas},
        "status": {
            "replicas": replicas,
            "availableReplicas": available,
            "conditions": list(conditions),
        },
    }


def event(name, namespace, reason, message, kind="Pod"):
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {"namespace": namespace},
        "involvedObject": {"kind": kind, "name": name, "namespace": namespace},
        "reason": reason,
        "message": message,
        "type": "Warning",
    }


pods = [
    # A rollout that crashes on startup. Two replicas down.
    pod(
        "checkout-7d9f4c8b6-x2klm", "payments", rs="checkout-7d9f4c8b6",
        containers=[{"name": "checkout", "image": "reg.internal/checkout:2.4.0",
                     "resources": {"limits": {"memory": "512Mi", "cpu": "500m"}}}],
        statuses=[container_status(
            "checkout", "reg.internal/checkout:2.4.0", ready=False, restarts=8,
            waiting={"reason": "CrashLoopBackOff",
                     "message": "back-off 5m0s restarting failed container=checkout"},
            last_terminated={"reason": "Error", "exitCode": 1,
                             "finishedAt": "2026-08-09T14:01:52Z"},
        )],
    ),
    pod(
        "checkout-7d9f4c8b6-p8trq", "payments", rs="checkout-7d9f4c8b6",
        containers=[{"name": "checkout", "image": "reg.internal/checkout:2.4.0",
                     "resources": {"limits": {"memory": "512Mi", "cpu": "500m"}}}],
        statuses=[container_status(
            "checkout", "reg.internal/checkout:2.4.0", ready=False, restarts=7,
            waiting={"reason": "CrashLoopBackOff",
                     "message": "back-off 5m0s restarting failed container=checkout"},
            last_terminated={"reason": "Error", "exitCode": 1,
                             "finishedAt": "2026-08-09T14:01:40Z"},
        )],
    ),
    # Memory limit too low: the kernel keeps reaping it.
    pod(
        "api-5c6d7f8a9b-mn4zx", "payments", rs="api-5c6d7f8a9b",
        containers=[{"name": "api", "image": "reg.internal/api:1.9.2",
                     "resources": {"limits": {"memory": "256Mi", "cpu": "1"}}}],
        statuses=[container_status(
            "api", "reg.internal/api:1.9.2", ready=True, restarts=3,
            last_terminated={"reason": "OOMKilled", "exitCode": 137,
                             "finishedAt": "2026-08-09T13:58:03Z"},
        )],
    ),
    # Image tag does not exist.
    pod(
        "report-worker-6b8c9d2e1f-qq7ws", "jobs", rs="report-worker-6b8c9d2e1f",
        containers=[{"name": "worker", "image": "reg.internal/report-worker:v3.1.7"}],
        statuses=[container_status(
            "worker", "reg.internal/report-worker:v3.1.7", ready=False, restarts=0,
            waiting={"reason": "ImagePullBackOff",
                     "message": "Back-off pulling image \"reg.internal/report-worker:v3.1.7\""},
        )],
    ),
    # References a ConfigMap that was never created. No safe automated fix.
    pod(
        "emailer-84f5b6c7d8-vv3nn", "jobs", rs="emailer-84f5b6c7d8",
        containers=[{"name": "emailer", "image": "reg.internal/emailer:1.0.4"}],
        statuses=[container_status(
            "emailer", "reg.internal/emailer:1.0.4", ready=False, restarts=0,
            waiting={"reason": "CreateContainerConfigError",
                     "message": "configmap \"emailer-smtp\" not found"},
        )],
    ),
    # Requests more memory than any node can offer.
    pod(
        "ingest-9f8e7d6c5b-hh2pp", "analytics", rs="ingest-9f8e7d6c5b", phase="Pending",
        containers=[{"name": "ingest", "image": "reg.internal/ingest:4.0.1",
                     "resources": {"requests": {"memory": "64Gi", "cpu": "8"}}}],
        conditions=[{
            "type": "PodScheduled", "status": "False", "reason": "Unschedulable",
            "message": "0/3 nodes are available: 3 Insufficient memory.",
        }],
    ),
    # Up and green, quietly restarting all day. The one nobody notices.
    pod(
        "frontend-3a2b1c9d8e-ss5mm", "web", rs="frontend-3a2b1c9d8e",
        containers=[{"name": "frontend", "image": "reg.internal/frontend:8.2.0"}],
        statuses=[container_status(
            "frontend", "reg.internal/frontend:8.2.0", ready=True, restarts=11,
            last_terminated={"reason": "Error", "exitCode": 143,
                             "finishedAt": "2026-08-09T13:44:20Z"},
        )],
    ),
    # Broken, and permanently off limits.
    pod(
        "coredns-76f75df574-abcde", "kube-system", rs="coredns-76f75df574",
        containers=[{"name": "coredns", "image": "registry.k8s.io/coredns/coredns:v1.11.1"}],
        statuses=[container_status(
            "coredns", "registry.k8s.io/coredns/coredns:v1.11.1", ready=False, restarts=6,
            waiting={"reason": "CrashLoopBackOff", "message": "back-off restarting failed container"},
            last_terminated={"reason": "Error", "exitCode": 1},
        )],
    ),
]

deployments = [
    deployment("checkout", "payments", replicas=3, available=1, revision=12, conditions=[{
        "type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded",
        "message": "ReplicaSet \"checkout-7d9f4c8b6\" has timed out progressing.",
    }]),
    deployment("api", "payments", replicas=4, available=4, revision=7),
    deployment("report-worker", "jobs", replicas=2, available=0, revision=4),
    deployment("emailer", "jobs", replicas=1, available=0, revision=1),
    deployment("ingest", "analytics", replicas=2, available=1),
    deployment("frontend", "web", replicas=6, available=6, revision=9),
    deployment("coredns", "kube-system", replicas=2, available=1, revision=5),
]

events = [
    event("ingest-9f8e7d6c5b-hh2pp", "analytics", "FailedScheduling",
          "0/3 nodes are available: 3 Insufficient memory."),
    event("checkout-7d9f4c8b6-x2klm", "payments", "BackOff",
          "Back-off restarting failed container"),
    event("report-worker-6b8c9d2e1f-qq7ws", "jobs", "Failed",
          "Failed to pull image: manifest unknown"),
]

snapshot = {
    "pods": {"items": pods},
    "deployments": {"items": deployments},
    "events": {"items": events},
}

if __name__ == "__main__":
    out = pathlib.Path(__file__).parent / "broken-cluster.json"
    out.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"wrote {out} ({len(pods)} pods, {len(deployments)} deployments)")
