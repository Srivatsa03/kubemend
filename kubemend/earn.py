"""Let a workload's own track record move its autonomy, within what policy allows.

The incident log already measures the thing that should decide this: how often
the agent's fix to a given workload failed to hold and had to be withdrawn.
Until now that number was reported to a human and nothing else. This module
makes it load-bearing, so an agent earns the right to act unattended on the
evidence of having been right before, on that workload, in that cluster.

Four decisions shape it, and each one is deliberately asymmetric.

**Demotion is fast, promotion is slow.** A single withdrawn fix drops the level
immediately; climbing back needs a long clean run. The two errors are not
equivalent: promoting too eagerly hands production to an agent that has not
earned it, while demoting too eagerly costs a human some review. One of those is
recoverable during an incident and the other is the incident.

**Policy sets the bounds and evidence moves inside them.** A record can never
promote past ``policy.earned_ceiling``, which defaults to the policy's own
starting level. So earned autonomy is *off* unless a policy explicitly opens
headroom for it, which matches the rule the rest of the gate already follows:
an unconfigured policy permits nothing.

**Evidence never touches a hard refusal.** Protected namespaces, disallowed
action kinds, blast radius and reversibility are decided before this runs and
are not negotiable by track record. A workload with a spotless history in
``kube-system`` is still refused. Good behaviour buys a shorter leash, not a
different rulebook.

**Nothing moves silently.** Every adjustment carries the sentence explaining it,
because an autonomy level that changed for reasons nobody can see is exactly the
property this project exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Autonomy

__all__ = ["Record", "Adjustment", "adjust"]

_RANK = {Autonomy.REPORT: 0, Autonomy.PROPOSE: 1, Autonomy.APPLY: 2}
_BY_RANK = {v: k for k, v in _RANK.items()}


@dataclass(frozen=True)
class Record:
    """What the log says about the agent's fixes to one workload.

    ``streak`` is the count of consecutive verified fixes since the last revert,
    which is the number promotion is argued from. A high lifetime ``verified``
    with a recent revert is not a good record; it is a workload that just broke
    the agent's assumptions, and the streak is what expresses that.
    """

    workload: str = ""
    committed: int = 0
    verified: int = 0
    reverted: int = 0
    streak: int = 0

    @property
    def revert_rate(self) -> float:
        return self.reverted / self.committed if self.committed else 0.0

    @property
    def empty(self) -> bool:
        return self.committed == 0


@dataclass(frozen=True)
class Adjustment:
    """An autonomy level, and why it is not the one policy started with."""

    level: Autonomy
    reason: str = ""
    promoted: bool = False
    demoted: bool = False

    @property
    def changed(self) -> bool:
        return self.promoted or self.demoted


def _step(level: Autonomy, by: int, floor: Autonomy, ceiling: Autonomy) -> Autonomy:
    rank = min(max(_RANK[level] + by, _RANK[floor]), _RANK[ceiling])
    return _BY_RANK[rank]


def adjust(
    base: Autonomy,
    record: Record,
    *,
    ceiling: Autonomy | None = None,
    promote_after: int = 10,
    min_sample: int = 5,
) -> Adjustment:
    """Move ``base`` up or down on the evidence in ``record``.

    ``ceiling`` is the highest level evidence may reach, and defaults to ``base``
    so that a caller who has not opted in gets promotion disabled rather than
    enabled. ``promote_after`` is the clean streak required to climb one level,
    and ``min_sample`` is the number of committed fixes below which a record is
    not treated as evidence of anything.

    Demotion has no such threshold. One withdrawn fix is enough, because the
    question a revert answers is not "is this workload usually fine" but "was
    the agent wrong about this workload recently", and it was.
    """
    top = base if ceiling is None else ceiling

    if record.empty:
        return Adjustment(base, "no history for this workload")

    # Demotion first: a recent revert outranks any amount of older success.
    #
    # It demotes to PROPOSE and stops there, rather than stepping down one level
    # each time. The floor is deliberate. A workload whose last fix was withdrawn
    # is a workload that is probably still broken, and REPORT would mean the
    # agent goes quiet on it exactly when a human most wants to see the proposed
    # change. A recent revert should mean "a person looks at the next one", never
    # "say nothing". If policy already sits at REPORT, that is policy's call and
    # this leaves it alone.
    if record.streak == 0 and record.reverted:
        lowered = min(base, Autonomy.PROPOSE, key=lambda a: _RANK[a])
        if lowered is base:
            return Adjustment(
                base,
                f"{record.reverted} withdrawn fix(es) here; already at {base.value}, "
                "which is where a human sees it",
            )
        return Adjustment(
            lowered,
            f"last fix here did not hold ({record.reverted} of {record.committed} "
            f"withdrawn); a human reviews the next one",
            demoted=True,
        )

    if record.committed < min_sample:
        return Adjustment(
            base,
            f"only {record.committed} fix(es) here, needs {min_sample} to count",
        )

    if record.streak >= promote_after:
        raised = _step(base, 1, Autonomy.REPORT, top)
        if raised is base:
            reason = (
                f"{record.streak} consecutive fixes held, already at the ceiling "
                f"policy allows ({top.value})"
            )
            return Adjustment(base, reason)
        return Adjustment(
            raised,
            f"{record.streak} consecutive fixes here held; earned {raised.value}",
            promoted=True,
        )

    return Adjustment(
        base,
        f"{record.streak} of {promote_after} clean fixes toward {_step(base, 1, Autonomy.REPORT, top).value}",
    )
