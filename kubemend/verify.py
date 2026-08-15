"""Watch a workload after a change, and undo the change if it did not help.

A remediation loop that stops at "committed" is only half a loop. The agent has
acted on a diagnosis that may have been wrong, and until something checks, the
cluster is in a state nobody has confirmed is better than the one it replaced.

What "better" means here is deliberately narrow: **the findings that motivated
the change are gone, and no new critical finding has appeared on that workload.**
Not "the workload is perfect" — a service can carry a warning for weeks — and not
"the pods are running", which is true moments before a crash loop starts.

Two decisions in the failure handling matter more than the polling.

*Silence is not success.* A poll that cannot reach the cluster proves nothing, so
it never counts toward recovery. The deadline still runs, and the result is
reported as indeterminate rather than quietly passing.

*Indeterminate does not trigger a revert.* Reverting on positive evidence of
continued failure is right. Reverting because the API server was briefly
unreachable would undo a fix that may have worked, on no evidence at all, and
turns a network blip into a second production change. So an unverifiable outcome
stops and asks for a human, which is the same thing the rest of this codebase
does whenever it cannot see clearly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from .model import Finding, Severity, Target
from .signals import detect

__all__ = ["Outcome", "Verification", "verify", "findings_for"]


class Outcome(str, Enum):
    RECOVERED = "recovered"          # the motivating findings are gone
    STILL_FAILING = "still_failing"  # they are still there, or worse ones are
    INDETERMINATE = "indeterminate"  # the cluster could not be read

    @property
    def should_revert(self) -> bool:
        """Only positive evidence of continued failure justifies undoing."""
        return self is Outcome.STILL_FAILING


@dataclass
class Verification:
    """What was observed while watching, and what it means."""

    target: Target
    outcome: Outcome
    waited: float = 0.0
    polls: int = 0
    errors: int = 0
    remaining: list[Finding] = field(default_factory=list)
    appeared: list[Finding] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.outcome is Outcome.RECOVERED

    def explain(self) -> str:
        if self.outcome is Outcome.RECOVERED:
            return f"recovered after {self.waited:.0f}s"
        if self.outcome is Outcome.INDETERMINATE:
            return (
                f"could not read the cluster on {self.errors} of {self.polls} checks; "
                "leaving the change in place for a human to judge"
            )
        detail = "; ".join(f.summary for f in (self.appeared or self.remaining)[:2])
        return f"still failing after {self.waited:.0f}s: {detail}"


def findings_for(snapshot: dict, target: Target) -> list[Finding]:
    """Findings about one workload, which is the only thing a change speaks to."""
    return [f for f in detect(snapshot) if f.target == target]


def verify(
    target: Target,
    read_snapshot,
    *,
    motivating: list[Finding] | None = None,
    timeout: float = 180.0,
    interval: float = 10.0,
    settle: float = 15.0,
    max_polls: int = 500,
    sleep=time.sleep,
    now=time.monotonic,
) -> Verification:
    """Poll until the workload recovers, the deadline passes, or it gets worse.

    ``read_snapshot`` is injected rather than imported so this is testable
    without a cluster, and so the caller decides whether it is reading live or
    replaying.

    ``max_polls`` bounds the loop independently of the clock. A deadline is the
    right way to stop, but it is the only way to stop, and a caller supplying a
    clock that does not advance would otherwise spin forever. Belt and braces on
    a loop that runs unattended against production is cheap.

    ``settle`` is a grace period before the first check. A reconciler needs a
    moment to apply the commit, and a rollout needs a moment to start; checking
    immediately would reliably observe the old state and call it a failure.

    Recovery must also *hold*: a rollout looks briefly healthy as it begins, so
    a workload is only declared recovered after two consecutive clean reads. A
    read that failed is not clean and not skipped — it resets the streak, since
    two clean reads either side of a blind one are not consecutive observations.
    """
    motivating_rules = {f.rule for f in (motivating or [])}
    deadline = now() + timeout
    result = Verification(target=target, outcome=Outcome.STILL_FAILING)
    started = now()
    clean_streak = 0

    sleep(settle)

    while True:
        result.polls += 1
        try:
            snapshot = read_snapshot()
        except StopIteration:
            # Only a test harness runs out of readings, and swallowing it as a
            # read error would hide the end of the script behind a timeout.
            raise
        except Exception:  # noqa: BLE001 - any read failure is the same to us
            # Proves nothing, so it cannot count as recovery. The deadline runs
            # on regardless, and enough of these make the result indeterminate.
            result.errors += 1
            snapshot = None
            # An unreadable poll breaks the recovery streak. Two clean reads
            # either side of a blind one are not two consecutive observations,
            # and letting silence bridge them would contradict the rule this
            # module is built on.
            clean_streak = 0

        if snapshot is not None:
            current = findings_for(snapshot, target)
            result.remaining = [f for f in current if f.rule in motivating_rules]
            # A different critical problem on the same workload is not recovery,
            # and is the shape a fix that made things worse would take.
            result.appeared = [
                f for f in current
                if f.rule not in motivating_rules and f.severity is Severity.CRITICAL
            ]
            if not result.remaining and not result.appeared:
                clean_streak += 1
                if clean_streak >= 2:
                    result.waited = now() - started
                    result.outcome = Outcome.RECOVERED
                    return result
            else:
                clean_streak = 0

        if now() >= deadline or result.polls >= max_polls:
            result.waited = now() - started
            # Every read failing means we never actually looked.
            if result.errors == result.polls:
                result.outcome = Outcome.INDETERMINATE
            elif result.errors and not result.remaining and not result.appeared:
                # Mixed reads that never confirmed recovery twice running.
                result.outcome = Outcome.INDETERMINATE
            else:
                result.outcome = Outcome.STILL_FAILING
            return result

        sleep(min(interval, max(0.0, deadline - now())))
