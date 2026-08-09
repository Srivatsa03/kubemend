"""Core types: what the agent observes, concludes, and is allowed to propose.

The central design decision lives in this file. An agent that emits shell
commands or raw manifests has an unbounded action space: you cannot enumerate
what it might do, you cannot compute the blast radius before it runs, and you
cannot mechanically undo it. Every safety property you would want is
unavailable in principle, not just unimplemented.

So the agent never emits commands. It selects from a **closed set of typed
actions**, and every action carries both the state it found and the state it
intends. Three properties follow directly, and the rest of the system is built
on them:

1. *Reversibility by construction.* An action holds ``before`` and ``after``, so
   its inverse is a field swap. An action whose ``before`` could not be captured
   is rejected before it is ever considered, rather than discovered to be
   irreversible at rollback time.
2. *Blast radius is computable before execution.* Every action declares the
   workloads and pods it touches, so a plan can be measured and refused while it
   is still text.
3. *Reviewability.* A typed action renders to a deterministic diff. A human
   reviewing a pull request sees a bounded change, not a prompt's output.

Nothing here talks to Kubernetes or to a model. These are plain values, which
keeps detection and policy pure and exhaustively testable against recorded
cluster state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Severity",
    "ActionKind",
    "Autonomy",
    "Target",
    "Finding",
    "Action",
    "Plan",
]


class Severity(str, Enum):
    """How bad the observed condition is, independent of confidence in the fix."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_ORDER = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


def at_least(severity: Severity, threshold: Severity) -> bool:
    return _SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[threshold]


class ActionKind(str, Enum):
    """The closed set of things the agent may propose.

    Deliberately small. Each entry is a change an experienced operator makes
    routinely, whose effect is local and whose reversal is mechanical. Anything
    outside this set is a human's job, and the correct behaviour for the agent
    is to explain rather than to act.

    Adding a kind here is a security decision, not a feature decision: it widens
    what an autonomous system can do to a live cluster.
    """

    SCALE = "scale"                      # change replica count
    ROLLBACK = "rollback"                # revert a workload to a prior revision
    RESTART = "restart"                  # trigger a rolling restart
    SET_RESOURCES = "set_resources"      # adjust requests/limits
    SET_IMAGE = "set_image"              # pin or correct a container image
    SET_PROBE = "set_probe"              # adjust probe timings/thresholds


class Autonomy(str, Enum):
    """How far a plan is permitted to travel without a human.

    Mirrors the crawl/walk/run progression operations teams actually use. The
    level is a property of policy, not of the agent, so trust is granted per
    action class and per environment rather than assumed globally.
    """

    REPORT = "report"    # write findings only; propose nothing
    PROPOSE = "propose"  # open a pull request for human review
    APPLY = "apply"      # commit to the GitOps branch unattended


@dataclass(frozen=True)
class Target:
    """What an action operates on. Namespaced by construction."""

    namespace: str
    kind: str  # Deployment, StatefulSet, DaemonSet
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.kind.lower()}/{self.name}"


@dataclass
class Finding:
    """One diagnosed problem, with the evidence that produced it.

    ``evidence`` holds the literal cluster facts the rule matched. It exists so
    a reviewer can check the conclusion without trusting the reasoning, which
    matters more here than usual: the diagnosis may later be written by a model,
    but the evidence is always gathered by code.
    """

    rule: str
    severity: Severity
    target: Target
    summary: str
    evidence: dict = field(default_factory=dict)
    # Pods currently exhibiting the problem. Feeds the blast-radius calculation.
    affected_pods: list[str] = field(default_factory=list)
    # Action kinds a human operator would plausibly reach for. Advisory only:
    # policy still decides whether any of them are permitted.
    suggests: list[ActionKind] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.rule}:{self.target}"


@dataclass
class Action:
    """A single typed change, carrying the state it found and the state it wants.

    ``before`` is not bookkeeping. It is what makes the action reversible, and
    the policy gate refuses any action that lacks it.
    """

    kind: ActionKind
    target: Target
    before: dict
    after: dict
    reason: str
    # Pods expected to be replaced or disrupted by applying this.
    impacted_pods: int = 0

    @property
    def reversible(self) -> bool:
        """True when the prior state was captured completely enough to restore.

        An empty ``before`` means the agent proposed a change without knowing
        what it was changing from. That is exactly the case where an automated
        rollback would silently do the wrong thing, so it is treated as
        irreversible rather than optimistically applied.
        """
        return bool(self.before) and set(self.before) == set(self.after)

    def inverse(self) -> Action:
        """The action that undoes this one."""
        if not self.reversible:
            raise ValueError(f"{self.kind.value} on {self.target} is not reversible")
        return Action(
            kind=self.kind,
            target=self.target,
            before=dict(self.after),
            after=dict(self.before),
            reason=f"revert: {self.reason}",
            impacted_pods=self.impacted_pods,
        )

    def describe(self) -> str:
        changes = ", ".join(
            f"{k}: {self.before.get(k)!r} -> {self.after.get(k)!r}" for k in sorted(self.after)
        )
        return f"{self.kind.value} {self.target} ({changes})"


@dataclass
class Plan:
    """An ordered set of actions answering one or more findings.

    A plan is the unit policy operates on, because the risk of a change is not
    a property of any single action: six restarts across six services is a very
    different event from one restart, even though each action is individually
    trivial.
    """

    findings: list[Finding] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    rationale: str = ""

    @property
    def targets(self) -> set[Target]:
        return {a.target for a in self.actions}

    @property
    def namespaces(self) -> set[str]:
        return {a.target.namespace for a in self.actions}

    @property
    def impacted_pods(self) -> int:
        return sum(a.impacted_pods for a in self.actions)

    @property
    def kinds(self) -> set[ActionKind]:
        return {a.kind for a in self.actions}

    @property
    def reversible(self) -> bool:
        return all(a.reversible for a in self.actions)

    def rollback(self) -> Plan:
        """The plan that undoes this one, applied in reverse order."""
        return Plan(
            findings=list(self.findings),
            actions=[a.inverse() for a in reversed(self.actions)],
            rationale=f"rollback of: {self.rationale}",
        )
