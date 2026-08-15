"""Tests for the incident log.

The behaviour that matters is longitudinal — what happened to this workload,
how often the agent's own fix failed — so most of these write several runs and
then ask a question no single run could answer.
"""

from __future__ import annotations

import pytest

from kubemend.journal import Journal
from kubemend.model import Action, ActionKind, Autonomy, Finding, Plan, Severity, Target
from kubemend.safety import CONSERVATIVE, gate
from kubemend.verify import Outcome, Verification

PAY = Target("payments", "Deployment", "checkout")


@pytest.fixture()
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def plan_for(target=PAY, kind=ActionKind.ROLLBACK):
    return Plan(
        findings=[Finding("crashloop", Severity.CRITICAL, target, "container crashing",
                          evidence={"restartCount": 8})],
        actions=[Action(kind, target, {"revision": 2}, {"revision": 1}, "bad rollout",
                        impacted_pods=3)],
        rationale="test",
    )


class FakeEmission:
    def __init__(self, commit="", branch="main", pr_url=""):
        self.commit, self.branch, self.pr_url, self.changes = commit, branch, pr_url, []


def verification(outcome):
    return Verification(target=PAY, outcome=outcome, waited=30.0, polls=3)


# --- recording ---------------------------------------------------------------


def test_an_incident_survives_a_reopen(journal, tmp_path):
    plan = plan_for()
    journal.record(journal.start_run("conservative", "", "snapshot"),
                   plan, gate(plan, CONSERVATIVE), FakeEmission("abc1234"))
    journal.close()

    reopened = Journal(tmp_path / "journal.db")
    rows = reopened.recent()
    assert len(rows) == 1
    assert rows[0].target == "payments/deployment/checkout"
    assert rows[0].commit_sha == "abc1234"


def test_refusals_are_recorded_too(journal):
    """What the agent declined to do is part of the audit trail."""
    plan = plan_for(Target("kube-system", "Deployment", "coredns"))
    verdict = gate(plan, CONSERVATIVE)
    assert not verdict.allowed
    journal.record(0, plan, verdict)
    row = journal.recent()[0]
    assert row.state == "refused"
    assert not row.allowed


def test_findings_and_actions_are_kept_with_the_incident(journal):
    plan = plan_for()
    incident = journal.record(0, plan, gate(plan, CONSERVATIVE), FakeEmission("abc1234"))
    findings = journal._query("SELECT * FROM findings WHERE incident_id = ?", (incident,))
    actions = journal._query("SELECT * FROM actions WHERE incident_id = ?", (incident,))
    assert findings[0]["rule"] == "crashloop"
    assert "restartCount" in findings[0]["evidence"]
    assert actions[0]["kind"] == "rollback"
    assert actions[0]["impacted_pods"] == 3


# --- the states an incident can end in ---------------------------------------


@pytest.mark.parametrize(
    "emission,verify_outcome,reverted,expected",
    [
        (None, None, "", "refused"),                                  # gate said no
        (FakeEmission(""), None, "", "reported"),                     # nothing written
        (FakeEmission("abc"), None, "", "committed"),                 # written, unverified
        (FakeEmission("abc"), Outcome.RECOVERED, "", "verified"),
        (FakeEmission("abc"), Outcome.INDETERMINATE, "", "indeterminate"),
        (FakeEmission("abc"), Outcome.STILL_FAILING, "def", "reverted"),
    ],
)
def test_each_incident_gets_one_clear_state(journal, emission, verify_outcome, reverted, expected):
    target = PAY if expected != "refused" else Target("kube-system", "Deployment", "coredns")
    plan = plan_for(target)
    journal.record(0, plan, gate(plan, CONSERVATIVE), emission,
                   verification(verify_outcome) if verify_outcome else None, reverted)
    assert journal.recent()[0].state == expected


# --- the questions a single run cannot answer --------------------------------


def test_history_is_per_workload_and_newest_first(journal):
    for _ in range(3):
        p = plan_for()
        journal.record(0, p, gate(p, CONSERVATIVE), FakeEmission("aaa"))
    other = plan_for(Target("web", "Deployment", "frontend"))
    journal.record(0, other, gate(other, CONSERVATIVE), FakeEmission("bbb"))

    rows = journal.history("payments", "checkout")
    assert len(rows) == 3
    assert rows[0].id > rows[-1].id


def test_repeat_offenders_surface_the_workload_that_keeps_breaking(journal):
    """A service remediated four times has a problem no rollback will fix."""
    for _ in range(4):
        p = plan_for()
        journal.record(0, p, gate(p, CONSERVATIVE), FakeEmission("aaa"))
    once = plan_for(Target("web", "Deployment", "frontend"))
    journal.record(0, once, gate(once, CONSERVATIVE), FakeEmission("bbb"))

    offenders = journal.repeat_offenders()
    assert offenders[0] == ("payments/checkout", 4)


def test_revert_rate_is_the_agents_own_accuracy(journal):
    """Three commits, one of which did not hold."""
    for outcome, reverted in ((Outcome.RECOVERED, ""), (Outcome.RECOVERED, ""),
                              (Outcome.STILL_FAILING, "def5678")):
        p = plan_for()
        journal.record(0, p, gate(p, CONSERVATIVE), FakeEmission("abc"),
                       verification(outcome), reverted)

    stats = journal.stats()
    assert stats.committed == 3
    assert stats.reverted == 1
    assert stats.revert_rate == pytest.approx(1 / 3)
    assert stats.verified_rate == pytest.approx(2 / 3)


def test_stats_on_an_empty_journal_do_not_divide_by_zero(journal):
    stats = journal.stats()
    assert stats.incidents == 0
    assert stats.revert_rate == 0.0


def test_runs_are_counted(journal):
    plan = plan_for()
    run = journal.start_run("conservative", "payments", "live")
    journal.record(run, plan, gate(plan, CONSERVATIVE), FakeEmission("abc"))
    journal.finish_run(run, findings=1, incidents=1, commits=1)
    assert journal.stats().runs == 1


# --- bookkeeping must never break a remediation ------------------------------


def test_an_unwritable_journal_degrades_instead_of_raising(tmp_path):
    """Failing to fix a cluster because a log file was read-only is a poor trade."""
    blocked = tmp_path / "nope"
    blocked.write_text("i am a file, not a directory")
    j = Journal(blocked / "journal.db")
    assert not j.available
    assert j.error
    # Every operation is still callable and simply does nothing.
    plan = plan_for()
    assert j.start_run("conservative", "", "snapshot") == 0
    assert j.record(0, plan, gate(plan, CONSERVATIVE)) == 0
    assert j.recent() == []
    assert j.stats().incidents == 0


def test_a_write_failure_mid_run_is_swallowed(journal):
    """A journal that breaks halfway must not take the remediation with it."""
    plan = plan_for()
    assert journal.record(0, plan, gate(plan, CONSERVATIVE), FakeEmission("abc"))

    journal._conn.close()          # the database goes away underneath us
    assert journal.record(0, plan, gate(plan, CONSERVATIVE)) == 0
    assert not journal.available
    assert journal.recent() == []  # reads degrade the same way
