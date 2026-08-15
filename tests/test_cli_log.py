"""Tests for `kubemend log`.

The command is thin, so these check the two things a thin command can still get
wrong: showing numbers that do not match the rows above them, and falling over
on an empty or unreadable log.
"""

from __future__ import annotations

import pytest

from kubemend.cli import main
from kubemend.journal import Journal
from kubemend.model import Action, ActionKind, Finding, Plan, Severity, Target
from kubemend.safety import CONSERVATIVE, gate
from kubemend.verify import Outcome, Verification

PAY = Target("payments", "Deployment", "checkout")
JOBS = Target("jobs", "Deployment", "report-worker")


class FakeEmission:
    def __init__(self, commit="", branch="main"):
        self.commit, self.branch, self.pr_url, self.changes = commit, branch, "", []


def plan_for(target, kind=ActionKind.ROLLBACK):
    return Plan(
        findings=[Finding("crashloop", Severity.CRITICAL, target, f"{target.name} crashing")],
        actions=[Action(kind, target, {"revision": 2}, {"revision": 1}, "bad rollout",
                        impacted_pods=2)],
        rationale="test",
    )


@pytest.fixture()
def populated(tmp_path):
    """Two workloads, one fix that held and one that had to be withdrawn."""
    path = tmp_path / "journal.db"
    j = Journal(path)
    run = j.start_run("conservative", "", "snapshot")

    kept = plan_for(PAY)
    j.record(run, kept, gate(kept, CONSERVATIVE), FakeEmission("aaaa111"),
             Verification(target=PAY, outcome=Outcome.RECOVERED, waited=30.0))

    undone = plan_for(PAY)
    j.record(run, undone, gate(undone, CONSERVATIVE), FakeEmission("bbbb222"),
             Verification(target=PAY, outcome=Outcome.STILL_FAILING, waited=75.0),
             reverted_sha="cccc333")

    other = plan_for(JOBS)
    j.record(run, other, gate(other, CONSERVATIVE), FakeEmission("dddd444"))

    j.finish_run(run, 3, 3, 3)
    j.close()
    return str(path)


def run_log(path, *extra):
    return main(["log", "--journal", path, "--no-color", *extra])


def test_an_empty_log_is_not_an_error(tmp_path, capsys):
    # Nothing to say is a normal state on a first run, not a failure.
    assert run_log(str(tmp_path / "fresh.db")) == 0
    assert "nothing recorded" in capsys.readouterr().out


def test_it_reports_the_revert_rate(populated, capsys):
    run_log(populated)
    out = capsys.readouterr().out
    # Three commits were written and one of them was withdrawn.
    assert "revert rate 33%" in out
    assert "1 of 3 fixes did not hold" in out


def test_reverted_incidents_read_as_reverted(populated, capsys):
    run_log(populated)
    out = capsys.readouterr().out
    assert "reverted" in out
    assert "verified" in out


def test_a_namespace_filter_hides_other_namespaces(populated, capsys):
    run_log(populated, "-n", "payments")
    out = capsys.readouterr().out
    assert "payments/deployment/checkout" in out
    assert "jobs/deployment/report-worker" not in out


def test_filtered_totals_say_they_are_not_filtered(populated, capsys):
    # The rows respect the filter and the totals do not, so the totals have to
    # admit it — an unlabelled revert rate under a namespace heading reads as
    # that namespace's revert rate.
    run_log(populated, "-n", "payments")
    assert "all workloads" in capsys.readouterr().out


def test_a_workload_filter_drops_the_repeat_offender_list(populated, capsys):
    run_log(populated, "-n", "payments", "--workload", "checkout")
    out = capsys.readouterr().out
    assert "keeps coming back" not in out
    assert "payments/deployment/checkout" in out


def test_repeat_offenders_surface_the_workload_seen_twice(populated, capsys):
    run_log(populated)
    out = capsys.readouterr().out
    offenders = out.split("keeps coming back")[1]
    assert "payments/checkout" in offenders
    # Seen once, so it is not a pattern yet.
    assert "jobs/report-worker" not in offenders


def test_a_workload_without_a_namespace_is_not_filtered_to_nothing(populated, capsys):
    run_log(populated, "--workload", "report-worker")
    assert "jobs/deployment/report-worker" in capsys.readouterr().out
