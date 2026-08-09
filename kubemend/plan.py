"""Turn findings into a proposed plan.

This layer is deliberately deterministic today. There is no model in it, and
that is a sequencing decision rather than a limitation: the parts that must be
trustworthy (what is wrong, and what is permitted) are code with tests, so when
a model is added it correlates and explains rather than deciding what happens to
the cluster.

Two rules govern everything here:

*Propose nothing you cannot undo.* An action is only emitted when the snapshot
contains the current state to put in ``before``. Where the data is missing, the
finding is reported and no action is proposed. That is why several findings in
the demo produce no action at all, which is the correct behaviour rather than a
gap.

*One action per workload.* Findings overlap heavily during a real incident: a
bad rollout produces a stuck rollout, two crash loops, and a replica shortfall,
all describing one event. Emitting an action per finding would quadruple the
apparent blast radius and could stack conflicting changes on one Deployment.
"""

from __future__ import annotations

import re

from .model import Action, ActionKind, Finding, Plan, Target

__all__ = ["propose", "unaddressed", "parse_quantity", "format_quantity"]

# Rules that indicate a recent rollout is at fault, in priority order. Rolling
# back moves the workload to a state that demonstrably ran in this cluster,
# which is why it is the action trusted earliest.
_ROLLBACK_RULES = ("rollout_stuck", "crashloop", "image_pull")

_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def parse_quantity(value: str) -> int | None:
    """Parse a Kubernetes memory quantity such as '256Mi' into bytes."""
    if not value:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([A-Za-z]*)", str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    if suffix and suffix not in _SUFFIXES:
        return None
    return int(float(number) * _SUFFIXES.get(suffix, 1))


def format_quantity(num_bytes: int) -> str:
    """Render bytes back to the largest binary suffix that stays whole."""
    for suffix in ("Gi", "Mi", "Ki"):
        unit = _SUFFIXES[suffix]
        if num_bytes >= unit and num_bytes % unit == 0:
            return f"{num_bytes // unit}{suffix}"
    return str(num_bytes)


def _deployments_by_target(snapshot: dict) -> dict[Target, dict]:
    section = snapshot.get("deployments") or {}
    items = section if isinstance(section, list) else section.get("items", []) or []
    out = {}
    for dep in items:
        meta = dep.get("metadata", {}) or {}
        target = Target(meta.get("namespace", "default"), "Deployment", meta.get("name", ""))
        out[target] = dep
    return out


def _revision(dep: dict) -> int | None:
    annotations = (dep.get("metadata", {}) or {}).get("annotations", {}) or {}
    raw = annotations.get("deployment.kubernetes.io/revision")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _replicas(dep: dict) -> int:
    return (dep.get("spec", {}) or {}).get("replicas", 0) or 0


def _rollback(target: Target, dep: dict, reason: str) -> Action | None:
    """Revert a workload to its previous rollout revision."""
    revision = _revision(dep)
    # Revision 1 has nothing behind it, and a missing annotation means we cannot
    # name the prior state. Either way there is no undo, so nothing is proposed.
    if revision is None or revision <= 1:
        return None
    return Action(
        kind=ActionKind.ROLLBACK,
        target=target,
        before={"revision": revision},
        after={"revision": revision - 1},
        reason=reason,
        impacted_pods=_replicas(dep),
    )


def _raise_memory(target: Target, finding: Finding, dep: dict) -> Action | None:
    """Double a memory limit the kernel is enforcing against the workload.

    Doubling is a stopgap and the finding records the original limit, because
    this is also exactly how a memory leak gets hidden for another week. It is
    proposed for review, never applied unattended, under every shipped policy.
    """
    limits = finding.evidence.get("limits") or {}
    current = parse_quantity(limits.get("memory", ""))
    if current is None:
        return None
    return Action(
        kind=ActionKind.SET_RESOURCES,
        target=target,
        before={"memory": limits["memory"]},
        after={"memory": format_quantity(current * 2)},
        reason=f"container was OOMKilled at {limits['memory']}",
        impacted_pods=_replicas(dep),
    )


def propose(findings: list[Finding], snapshot: dict) -> list[Plan]:
    """Build one plan per affected workload.

    Planning per workload rather than per cluster is the important choice here.
    An incident is something that happened to a service, and a human responder
    handles them one at a time; batching every unrelated problem in the cluster
    into a single change would trip every blast-radius limit at once and, worse,
    would couple the fate of unrelated services to one another. Separate plans
    are also separately reviewable, separately appliable, and separately
    revertible.

    Findings with no safe, reversible fix produce no plan. They still surface in
    the report, which is where they belong.
    """
    deployments = _deployments_by_target(snapshot)
    by_target: dict[Target, list[Finding]] = {}
    for finding in findings:
        by_target.setdefault(finding.target, []).append(finding)

    plans: list[Plan] = []
    for target, group in by_target.items():
        dep = deployments.get(target)
        if dep is None:
            continue  # bare pod, or a workload kind with no typed action

        rules = {f.rule for f in group}
        action: Action | None = None
        source: Finding | None = None

        # A recent rollout at fault outranks everything else: rolling back both
        # undoes the cause and clears the symptoms it produced.
        for rule in _ROLLBACK_RULES:
            if rule in rules:
                source = next(f for f in group if f.rule == rule)
                action = _rollback(target, dep, f"{rule}: {source.summary}")
                break

        if action is None and "oomkilled" in rules:
            source = next(f for f in group if f.rule == "oomkilled")
            action = _raise_memory(target, source, dep)

        if action is None or source is None:
            continue

        plans.append(
            Plan(
                findings=group,
                actions=[action],
                rationale=f"{target}: {source.rule}",
            )
        )

    plans.sort(key=lambda p: str(next(iter(p.targets))))
    return plans


def unaddressed(findings: list[Finding], plans: list[Plan]) -> list[Finding]:
    """Findings no plan covers, i.e. the ones a human still has to look at."""
    covered = {t for plan in plans for t in plan.targets}
    return [f for f in findings if f.target not in covered]
