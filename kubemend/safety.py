"""The policy gate: what the agent is permitted to propose, and how far it travels.

Diagnosis is the easy half. Every AI SRE product on the market will read your
cluster and tell you what it thinks. The reason almost none of them are trusted
to *act* is that nobody has a convincing answer to "what stops it doing
something catastrophic at 3am", and "the model is usually careful" is not an
answer.

This module is that answer, and it is deliberately boring: a pure function from
(plan, policy) to a verdict, with no model in the loop. The properties it
enforces are the ones an operator would demand of a junior engineer with
production access on their first week:

- Stay out of the namespaces that run the cluster itself.
- Only do things from a list agreed in advance.
- Do not touch more than N things at once, however confident you are.
- Never make a change you cannot undo.
- Slow down if you have already changed a lot today.
- Ask a human for anything above your pay grade.

Two decisions are worth stating explicitly because they are where this kind of
system usually goes wrong:

*The plan's autonomy is the minimum across its actions.* One action requiring
review holds back the whole plan. The alternative, letting safe actions proceed
while risky ones wait, splits a plan that was reasoned about as a unit and
applies half a fix, which is frequently worse than applying none.

*Everything is denied unless permitted.* The defaults below refuse more than
they allow. A permissive default in this file is not a usability question, it is
the difference between a bounded assistant and an unsupervised process with
cluster credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .earn import Record, adjust
from .model import Action, ActionKind, Autonomy, Plan

__all__ = ["Policy", "Violation", "Verdict", "gate", "CONSERVATIVE", "STAGING"]

# Namespaces that run the cluster itself. Touching these can remove the very
# machinery that would let you recover, so they are excluded outright rather
# than guarded by a threshold.
CONTROL_PLANE_NAMESPACES = frozenset({
    "kube-system", "kube-public", "kube-node-lease", "cert-manager",
    "ingress-nginx", "istio-system", "linkerd", "argocd", "flux-system",
    "monitoring", "observability", "velero",
})

_AUTONOMY_RANK = {Autonomy.REPORT: 0, Autonomy.PROPOSE: 1, Autonomy.APPLY: 2}


@dataclass(frozen=True)
class Violation:
    """One reason a plan was refused or held back, in machine-readable form.

    ``code`` is stable so callers can branch on it; ``remedy`` exists so the
    report can tell an operator what to change instead of only what failed.
    """

    code: str
    message: str
    action: str = ""
    remedy: str = ""


@dataclass
class Policy:
    """The operator's standing instructions. Data, not code, so it lives in git.

    Keeping policy as a plain value means the rules governing an autonomous
    system are themselves reviewable, diffable and revertible, which is the same
    property the agent's actions are required to have.
    """

    protected_namespaces: frozenset[str] = CONTROL_PLANE_NAMESPACES
    # Action kinds permitted at all. Absent means never, regardless of autonomy.
    allowed_kinds: frozenset[ActionKind] = frozenset()
    # Per-kind ceiling on how far an action may travel. Missing kinds fall back
    # to default_autonomy, which is the most restrictive level.
    autonomy: dict[ActionKind, Autonomy] = field(default_factory=dict)
    default_autonomy: Autonomy = Autonomy.REPORT
    # The highest level a workload's track record may earn for a given kind.
    # Absent means the starting level is also the ceiling, so earned autonomy
    # is off unless a policy deliberately opens headroom. Same principle as
    # allowed_kinds: nothing is permitted by omission.
    earned_ceiling: dict[ActionKind, Autonomy] = field(default_factory=dict)
    # Blast radius. These bound a single plan, not a single action, because the
    # risk of a change is a property of the whole set.
    max_impacted_pods: int = 5
    max_workloads: int = 1
    max_namespaces: int = 1
    require_reversible: bool = True
    # Flap protection: how many plans may already have been applied in the
    # current window before the agent must stand down and let a human look.
    max_plans_per_window: int = 3

    def ceiling(self, kind: ActionKind) -> Autonomy:
        return self.autonomy.get(kind, self.default_autonomy)

    def earned_top(self, kind: ActionKind) -> Autonomy:
        """How far evidence may raise this kind, never below its start."""
        top = self.earned_ceiling.get(kind, self.ceiling(kind))
        return max(top, self.ceiling(kind), key=lambda a: _AUTONOMY_RANK[a])


@dataclass
class Verdict:
    """The gate's decision: whether to proceed, and how far."""

    allowed: bool
    autonomy: Autonomy
    violations: list[Violation] = field(default_factory=list)

    @property
    def blocked_by(self) -> list[str]:
        return [v.code for v in self.violations]

    def explain(self) -> str:
        if self.allowed and not self.violations:
            return f"permitted at autonomy '{self.autonomy.value}'"
        if self.allowed:
            held = "; ".join(v.message for v in self.violations)
            return f"held at autonomy '{self.autonomy.value}': {held}"
        return "refused: " + "; ".join(v.message for v in self.violations)


def gate(plan: Plan, policy: Policy, recent_plans: int = 0,
         record: "Record | None" = None) -> Verdict:
    """Evaluate a plan against policy.

    ``recent_plans`` is supplied by the caller rather than read from a clock, so
    this stays a pure function and the flap-protection rule can be tested
    without waiting for time to pass. ``record`` is passed in for the same
    reason: the workload's track record comes from the journal, and the gate
    stays a function of its arguments rather than of a database.

    A verdict may be *allowed but held*: the plan is legitimate, but something
    about it caps how far it travels, so it becomes a pull request instead of a
    commit. That middle state is the one that makes the system usable, and it
    carries violations explaining the downgrade.
    """
    violations: list[Violation] = []

    # An empty plan is not an error. Detection ran and concluded there is
    # nothing worth doing, which is a perfectly good outcome.
    if not plan.actions:
        return Verdict(allowed=True, autonomy=Autonomy.REPORT)

    # --- hard refusals: no autonomy level makes these acceptable ---

    for action in plan.actions:
        if action.target.namespace in policy.protected_namespaces:
            violations.append(
                Violation(
                    code="protected_namespace",
                    message=f"{action.target.namespace} is protected",
                    action=action.describe(),
                    remedy="remove the namespace from policy.protected_namespaces to allow this",
                )
            )
        if action.kind not in policy.allowed_kinds:
            violations.append(
                Violation(
                    code="action_not_allowed",
                    message=f"action '{action.kind.value}' is not permitted by policy",
                    action=action.describe(),
                    remedy=f"add ActionKind.{action.kind.name} to policy.allowed_kinds",
                )
            )
        if policy.require_reversible and not action.reversible:
            violations.append(
                Violation(
                    code="irreversible",
                    message=f"action '{action.kind.value}' on {action.target} cannot be undone",
                    action=action.describe(),
                    remedy="capture the current state in Action.before so an inverse exists",
                )
            )

    # --- blast radius: bounded on the plan as a whole ---

    if plan.impacted_pods > policy.max_impacted_pods:
        violations.append(
            Violation(
                code="blast_radius_pods",
                message=f"plan disrupts {plan.impacted_pods} pods, limit is {policy.max_impacted_pods}",
                remedy="split the plan, or raise policy.max_impacted_pods deliberately",
            )
        )
    if len(plan.targets) > policy.max_workloads:
        violations.append(
            Violation(
                code="blast_radius_workloads",
                message=f"plan touches {len(plan.targets)} workloads, limit is {policy.max_workloads}",
                remedy="address one workload per plan, or raise policy.max_workloads",
            )
        )
    if len(plan.namespaces) > policy.max_namespaces:
        violations.append(
            Violation(
                code="blast_radius_namespaces",
                message=f"plan spans {len(plan.namespaces)} namespaces, limit is {policy.max_namespaces}",
                remedy="a change crossing namespaces is usually a human's call",
            )
        )

    # A cluster that has needed several fixes in one window is not a cluster to
    # keep fixing automatically. Repeated automated remediation is how a small
    # problem becomes an outage, so the agent stands down and reports instead.
    if recent_plans >= policy.max_plans_per_window:
        violations.append(
            Violation(
                code="rate_limited",
                message=(
                    f"{recent_plans} plans already applied this window "
                    f"(limit {policy.max_plans_per_window}); standing down"
                ),
                remedy="a human should look at why remediation keeps firing",
            )
        )

    if violations:
        return Verdict(allowed=False, autonomy=Autonomy.REPORT, violations=violations)

    # --- permitted: decide how far it travels ---
    #
    # The most restricted action governs the plan. Applying the safe half of a
    # plan that was reasoned about as a unit is usually worse than applying none.
    ceiling = min(
        (policy.ceiling(a.kind) for a in plan.actions),
        key=lambda level: _AUTONOMY_RANK[level],
    )
    held: list[Violation] = []
    if ceiling is not Autonomy.APPLY:
        limiting = sorted(
            {a.kind.value for a in plan.actions if policy.ceiling(a.kind) is ceiling}
        )
        held.append(
            Violation(
                code="autonomy_ceiling",
                message=f"held at '{ceiling.value}' by action(s): {', '.join(limiting)}",
                remedy="raise policy.autonomy for those kinds once they have proven safe",
            )
        )

    # Everything above is policy. This is the workload's own record, applied
    # last and only to the autonomy level: a hard refusal has already returned,
    # so nothing here can talk its way past one. The headroom is the lowest
    # earned ceiling across the plan's actions, for the same reason the starting
    # level is the lowest: one action that has not earned its place holds the
    # whole plan back.
    if record is not None and not record.empty:
        headroom = min(
            (policy.earned_top(a.kind) for a in plan.actions),
            key=lambda level: _AUTONOMY_RANK[level],
        )
        moved = adjust(ceiling, record, ceiling=headroom)
        if moved.changed:
            held.append(
                Violation(
                    # Two codes rather than one, so a reader (and the CLI) can
                    # tell which direction the evidence moved things without
                    # parsing the sentence.
                    code="earned_promotion" if moved.promoted else "earned_demotion",
                    message=moved.reason,
                    remedy=(
                        "this level came from the incident log, not the policy file; "
                        "kubemend log --workload <name> shows the record behind it"
                    ),
                )
            )
            ceiling = moved.level

    return Verdict(allowed=True, autonomy=ceiling, violations=held)


# --- shipped policies --------------------------------------------------------
#
# These are examples with opinions, not neutral defaults. The conservative one
# is what a team should start from on day one; loosening it should be a
# deliberate, reviewed commit rather than something that happens by accident.

CONSERVATIVE = Policy(
    # Rollback is the one action safe enough to trust early: it moves a workload
    # to a state that demonstrably ran in this cluster before, and its inverse is
    # exact. Everything else is proposed for review.
    allowed_kinds=frozenset({
        ActionKind.ROLLBACK, ActionKind.RESTART,
        ActionKind.SCALE, ActionKind.SET_RESOURCES,
    }),
    autonomy={
        ActionKind.ROLLBACK: Autonomy.APPLY,
        ActionKind.RESTART: Autonomy.PROPOSE,
        ActionKind.SCALE: Autonomy.PROPOSE,
        ActionKind.SET_RESOURCES: Autonomy.PROPOSE,
    },
    # Headroom a workload may earn on its own record. This is what the
    # conservative policy is *for*: start everything under review, and let the
    # ones that keep being right stop needing a human. A workload with no
    # history gets none of it, and one withdrawn fix takes it back.
    earned_ceiling={
        ActionKind.SCALE: Autonomy.APPLY,
        ActionKind.SET_RESOURCES: Autonomy.APPLY,
    },
    default_autonomy=Autonomy.REPORT,
    max_impacted_pods=5,
    max_workloads=1,
    max_namespaces=1,
    require_reversible=True,
    max_plans_per_window=3,
)

STAGING = Policy(
    allowed_kinds=frozenset({
        ActionKind.ROLLBACK, ActionKind.RESTART, ActionKind.SCALE,
        ActionKind.SET_RESOURCES, ActionKind.SET_IMAGE, ActionKind.SET_PROBE,
    }),
    autonomy={k: Autonomy.APPLY for k in ActionKind},
    default_autonomy=Autonomy.PROPOSE,
    max_impacted_pods=25,
    max_workloads=5,
    max_namespaces=2,
    require_reversible=True,
    max_plans_per_window=10,
)
