"""Detection rules: cluster state in, findings out.

Every rule here is a pure function over the JSON that ``kubectl get -o json``
already returns. That is a deliberate constraint rather than a convenience:

- Detection can be tested exhaustively against recorded cluster state, with no
  cluster, no model, and no cost.
- The evidence attached to a finding is gathered by code, so a reviewer can
  check a conclusion without trusting anything a model said about it.
- When a model is later added to explain and correlate findings, it is reasoning
  over facts that were established deterministically, not producing them.

The rules encode ordinary operational knowledge: what CrashLoopBackOff means
versus OOMKilled, why a pod stuck Pending is usually a scheduling problem rather
than an application problem. None of it is novel. It is the layer that has to be
right before anything above it can be trusted.
"""

from __future__ import annotations

from .model import ActionKind, Finding, Severity, Target

__all__ = ["detect", "RULES", "deployment_of"]

# Restarts above this are treated as a problem even when the pod is currently up:
# a container that has died a dozen times is failing intermittently, which is
# harder to catch and often worse than one that is cleanly down.
RESTART_THRESHOLD = 5


# --- snapshot navigation -----------------------------------------------------


def _items(snapshot: dict, key: str) -> list[dict]:
    section = snapshot.get(key) or {}
    if isinstance(section, list):
        return section
    return section.get("items", []) or []


def _meta(obj: dict) -> dict:
    return obj.get("metadata", {}) or {}


def _name(obj: dict) -> str:
    return _meta(obj).get("name", "")


def _namespace(obj: dict) -> str:
    return _meta(obj).get("namespace", "default")


def deployment_of(pod: dict) -> str | None:
    """Resolve the Deployment a pod belongs to, via its ReplicaSet owner.

    Kubernetes names a Deployment's ReplicaSets ``<deployment>-<pod-template-hash>``,
    so stripping the final segment recovers the Deployment. Pods owned directly
    by a Job, DaemonSet or nothing at all return None and are reported against
    themselves, since there is no workload to act on.
    """
    for ref in _meta(pod).get("ownerReferences", []) or []:
        if ref.get("kind") == "ReplicaSet":
            rs_name = ref.get("name", "")
            head, sep, tail = rs_name.rpartition("-")
            # Only strip when the tail looks like a generated hash, so a
            # ReplicaSet named "api-v2" is not mangled into "api".
            if sep and tail and tail.isalnum() and not tail.isdigit() and len(tail) >= 5:
                return head
            return rs_name or None
    return None


def _target_for_pod(pod: dict) -> Target:
    dep = deployment_of(pod)
    if dep:
        return Target(_namespace(pod), "Deployment", dep)
    return Target(_namespace(pod), "Pod", _name(pod))


def _container_statuses(pod: dict) -> list[dict]:
    return (pod.get("status", {}) or {}).get("containerStatuses", []) or []


def _events_for(snapshot: dict, name: str, namespace: str) -> list[dict]:
    out = []
    for ev in _items(snapshot, "events"):
        obj = ev.get("involvedObject", {}) or {}
        if obj.get("name") == name and obj.get("namespace", "default") == namespace:
            out.append(ev)
    return out


# --- rules -------------------------------------------------------------------
#
# Each rule takes the whole snapshot and returns findings. Rules are independent
# and may legitimately both fire on the same pod: a container can be both
# OOMKilled and in CrashLoopBackOff, and those are two different statements
# (why it died, and what the kubelet is now doing about it).


def rule_crashloop(snapshot: dict) -> list[Finding]:
    """Containers the kubelet has given up restarting promptly."""
    findings = []
    for pod in _items(snapshot, "pods"):
        for cs in _container_statuses(pod):
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            if waiting.get("reason") != "CrashLoopBackOff":
                continue
            findings.append(
                Finding(
                    rule="crashloop",
                    severity=Severity.CRITICAL,
                    target=_target_for_pod(pod),
                    summary=(
                        f"container {cs.get('name')} in {_name(pod)} is in CrashLoopBackOff "
                        f"after {cs.get('restartCount', 0)} restarts"
                    ),
                    evidence={
                        "pod": _name(pod),
                        "container": cs.get("name"),
                        "restartCount": cs.get("restartCount", 0),
                        "message": waiting.get("message", ""),
                        "exitCode": ((cs.get("lastState", {}) or {}).get("terminated", {}) or {}).get("exitCode"),
                    },
                    affected_pods=[_name(pod)],
                    # A crash loop that began after a rollout is a rollback
                    # candidate; one that did not is usually a config problem a
                    # restart will not fix, so both are offered and policy plus
                    # the correlation step decide.
                    suggests=[ActionKind.ROLLBACK, ActionKind.RESTART],
                )
            )
    return findings


def rule_oomkilled(snapshot: dict) -> list[Finding]:
    """Containers the kernel killed for exceeding their memory limit."""
    findings = []
    for pod in _items(snapshot, "pods"):
        for cs in _container_statuses(pod):
            last = (cs.get("lastState", {}) or {}).get("terminated", {}) or {}
            if last.get("reason") != "OOMKilled":
                continue
            findings.append(
                Finding(
                    rule="oomkilled",
                    severity=Severity.CRITICAL,
                    target=_target_for_pod(pod),
                    summary=f"container {cs.get('name')} in {_name(pod)} was OOMKilled",
                    evidence={
                        "pod": _name(pod),
                        "container": cs.get("name"),
                        "restartCount": cs.get("restartCount", 0),
                        "limits": _limits_for(pod, cs.get("name", "")),
                    },
                    affected_pods=[_name(pod)],
                    # Raising the limit is the mechanical fix. It is also how you
                    # paper over a leak, which is why this stays a suggestion and
                    # the finding records the current limit as evidence.
                    suggests=[ActionKind.SET_RESOURCES],
                )
            )
    return findings


def _limits_for(pod: dict, container: str) -> dict:
    for spec in (pod.get("spec", {}) or {}).get("containers", []) or []:
        if spec.get("name") == container:
            return (spec.get("resources", {}) or {}).get("limits", {}) or {}
    return {}


def rule_image_pull(snapshot: dict) -> list[Finding]:
    """Images the kubelet cannot fetch: bad tag, bad registry, or missing auth."""
    findings = []
    for pod in _items(snapshot, "pods"):
        for cs in _container_statuses(pod):
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            if waiting.get("reason") not in ("ImagePullBackOff", "ErrImagePull"):
                continue
            findings.append(
                Finding(
                    rule="image_pull",
                    severity=Severity.CRITICAL,
                    target=_target_for_pod(pod),
                    summary=f"cannot pull image {cs.get('image')} for {_name(pod)}",
                    evidence={
                        "pod": _name(pod),
                        "container": cs.get("name"),
                        "image": cs.get("image"),
                        "reason": waiting.get("reason"),
                        "message": waiting.get("message", ""),
                    },
                    affected_pods=[_name(pod)],
                    suggests=[ActionKind.ROLLBACK, ActionKind.SET_IMAGE],
                )
            )
    return findings


def rule_config_error(snapshot: dict) -> list[Finding]:
    """References to a ConfigMap or Secret that does not exist.

    Deliberately suggests nothing. The fix is to create or correct an object the
    agent has no typed action for, and inventing config values is precisely the
    class of change an autonomous system should never make. Reporting it clearly
    and stopping is the correct behaviour, and this rule exists partly to prove
    the system has that path.
    """
    findings = []
    for pod in _items(snapshot, "pods"):
        for cs in _container_statuses(pod):
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            if waiting.get("reason") not in ("CreateContainerConfigError", "InvalidImageName"):
                continue
            findings.append(
                Finding(
                    rule="config_error",
                    severity=Severity.CRITICAL,
                    target=_target_for_pod(pod),
                    summary=f"{_name(pod)} cannot start: {waiting.get('message', 'configuration error')}",
                    evidence={
                        "pod": _name(pod),
                        "container": cs.get("name"),
                        "reason": waiting.get("reason"),
                        "message": waiting.get("message", ""),
                    },
                    affected_pods=[_name(pod)],
                    suggests=[],
                )
            )
    return findings


def rule_unschedulable(snapshot: dict) -> list[Finding]:
    """Pods the scheduler cannot place, usually for want of capacity."""
    findings = []
    for pod in _items(snapshot, "pods"):
        status = pod.get("status", {}) or {}
        if status.get("phase") != "Pending":
            continue
        reasons = [
            c for c in (status.get("conditions", []) or [])
            if c.get("type") == "PodScheduled" and c.get("status") == "False"
        ]
        if not reasons:
            continue
        detail = reasons[0].get("message", "")
        events = _events_for(snapshot, _name(pod), _namespace(pod))
        sched = [e.get("message", "") for e in events if e.get("reason") == "FailedScheduling"]
        findings.append(
            Finding(
                rule="unschedulable",
                severity=Severity.WARNING,
                target=_target_for_pod(pod),
                summary=f"{_name(pod)} cannot be scheduled: {detail or (sched[0] if sched else 'no node fits')}",
                evidence={
                    "pod": _name(pod),
                    "reason": reasons[0].get("reason", ""),
                    "message": detail,
                    "events": sched[:3],
                },
                affected_pods=[_name(pod)],
                # Either the workload asks for too much, or too many replicas
                # were requested for the cluster. Both are in the action set.
                suggests=[ActionKind.SET_RESOURCES, ActionKind.SCALE],
            )
        )
    return findings


def rule_flapping(snapshot: dict) -> list[Finding]:
    """Containers restarting repeatedly while still reporting healthy.

    The pod looks up, so dashboards stay green and nothing pages. This is the
    failure mode most likely to survive unnoticed for weeks.
    """
    findings = []
    for pod in _items(snapshot, "pods"):
        for cs in _container_statuses(pod):
            restarts = cs.get("restartCount", 0)
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            # Crash loops are reported by their own rule; this is the quieter case.
            if restarts < RESTART_THRESHOLD or waiting.get("reason") == "CrashLoopBackOff":
                continue
            findings.append(
                Finding(
                    rule="flapping",
                    severity=Severity.WARNING,
                    target=_target_for_pod(pod),
                    summary=f"container {cs.get('name')} in {_name(pod)} has restarted {restarts} times",
                    evidence={
                        "pod": _name(pod),
                        "container": cs.get("name"),
                        "restartCount": restarts,
                        "lastTerminated": (cs.get("lastState", {}) or {}).get("terminated", {}),
                    },
                    affected_pods=[_name(pod)],
                    suggests=[ActionKind.SET_PROBE, ActionKind.ROLLBACK],
                )
            )
    return findings


def rule_rollout_stuck(snapshot: dict) -> list[Finding]:
    """Deployments whose rollout has exceeded its progress deadline."""
    findings = []
    for dep in _items(snapshot, "deployments"):
        for cond in (dep.get("status", {}) or {}).get("conditions", []) or []:
            if cond.get("type") == "Progressing" and cond.get("reason") == "ProgressDeadlineExceeded":
                findings.append(
                    Finding(
                        rule="rollout_stuck",
                        severity=Severity.CRITICAL,
                        target=Target(_namespace(dep), "Deployment", _name(dep)),
                        summary=f"rollout of {_name(dep)} exceeded its progress deadline",
                        evidence={
                            "deployment": _name(dep),
                            "message": cond.get("message", ""),
                            "replicas": (dep.get("spec", {}) or {}).get("replicas"),
                            "available": (dep.get("status", {}) or {}).get("availableReplicas", 0),
                        },
                        suggests=[ActionKind.ROLLBACK],
                    )
                )
    return findings


def rule_replica_shortfall(snapshot: dict) -> list[Finding]:
    """Deployments running fewer replicas than they are supposed to.

    Skipped when the rollout is already flagged as stuck, since that finding
    explains this one and reporting both would double-count the same incident.
    """
    stuck = {f.target for f in rule_rollout_stuck(snapshot)}
    findings = []
    for dep in _items(snapshot, "deployments"):
        target = Target(_namespace(dep), "Deployment", _name(dep))
        if target in stuck:
            continue
        desired = (dep.get("spec", {}) or {}).get("replicas")
        available = (dep.get("status", {}) or {}).get("availableReplicas", 0) or 0
        if desired is None or available >= desired:
            continue
        findings.append(
            Finding(
                rule="replica_shortfall",
                severity=Severity.CRITICAL if available == 0 else Severity.WARNING,
                target=target,
                summary=f"{_name(dep)} has {available}/{desired} replicas available",
                evidence={"deployment": _name(dep), "desired": desired, "available": available},
                suggests=[ActionKind.ROLLBACK] if available == 0 else [],
            )
        )
    return findings


RULES = [
    rule_crashloop,
    rule_oomkilled,
    rule_image_pull,
    rule_config_error,
    rule_unschedulable,
    rule_flapping,
    rule_rollout_stuck,
    rule_replica_shortfall,
]


def detect(snapshot: dict) -> list[Finding]:
    """Run every rule over a cluster snapshot, worst first.

    Findings are sorted by severity so that a truncated report still leads with
    what matters, and by target within a severity so output is stable across
    runs and diffs cleanly.
    """
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(snapshot))
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: (order[f.severity], str(f.target), f.rule))
    return findings
