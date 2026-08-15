"""Tests for the console.

Most of these go through a real socket rather than calling the router directly.
That is deliberate: the first bug this server had was that SQLite connections
are bound to their creating thread, so every request after the first returned an
empty list — and returned it *quietly*, because the journal's read path swallows
errors on purpose. Calling `_payload` in-process would have passed happily.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kubemend.journal import Journal
from kubemend.model import Action, ActionKind, Finding, Plan, Severity, Target
from kubemend.safety import CONSERVATIVE, gate
from kubemend.serve import build_handler
from kubemend.verify import Outcome, Verification

PAY = Target("payments", "Deployment", "checkout")
JOBS = Target("jobs", "Deployment", "report-worker")


class FakeEmission:
    def __init__(self, commit="", branch="main"):
        self.commit, self.branch, self.pr_url, self.changes = commit, branch, "", []


def plan_for(target):
    return Plan(
        findings=[Finding("image_pull", Severity.CRITICAL, target, f"cannot pull {target.name}",
                          evidence={"image": "reg/x:1", "reason": "ImagePullBackOff"})],
        actions=[Action(ActionKind.ROLLBACK, target, {"revision": 2}, {"revision": 1},
                        "bad release", impacted_pods=2)],
        rationale="test",
    )


@pytest.fixture()
def console(tmp_path):
    """A server on an ephemeral port, over a journal with one kept and one undone fix."""
    path = tmp_path / "journal.db"
    j = Journal(path)
    run = j.start_run("conservative", "", "live")
    kept = plan_for(PAY)
    j.record(run, kept, gate(kept, CONSERVATIVE), FakeEmission("aaaa111"),
             Verification(target=PAY, outcome=Outcome.RECOVERED, waited=46.0))
    undone = plan_for(PAY)
    j.record(run, undone, gate(undone, CONSERVATIVE), FakeEmission("bbbb222"),
             Verification(target=PAY, outcome=Outcome.STILL_FAILING, waited=75.0),
             reverted_sha="cccc333")
    other = plan_for(JOBS)
    j.record(run, other, gate(other, CONSERVATIVE))
    j.finish_run(run, 3, 3, 2)
    j.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(path))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def get(base, path):
    with urlopen(base + path, timeout=5) as r:  # noqa: S310 - our own localhost server
        return r.status, json.loads(r.read())


def test_the_page_is_served(console):
    with urlopen(console + "/", timeout=5) as r:  # noqa: S310
        body = r.read().decode()
    assert r.status == 200
    assert "<title>kubemend</title>" in body


def test_repeated_requests_keep_returning_data(console):
    """The threading bug: request one worked, everything after it came back empty."""
    for _ in range(4):
        status, body = get(console, "/api/incidents")
        assert status == 200
        assert len(body) == 3


def test_summary_reports_the_revert_rate(console):
    _, body = get(console, "/api/summary")
    assert body["committed"] == 2
    assert body["reverted"] == 1
    assert body["revert_rate"] == 0.5
    assert {"workload": "payments/checkout", "count": 2} in body["repeat_offenders"]


def test_incidents_can_be_filtered(console):
    _, all_rows = get(console, "/api/incidents")
    _, pay = get(console, "/api/incidents?namespace=payments")
    _, one = get(console, "/api/incidents?namespace=payments&workload=checkout")
    assert len(all_rows) == 3
    assert len(pay) == 2
    assert len(one) == 2
    assert all(r["target"].startswith("payments/") for r in pay)


def test_a_nonsense_limit_does_not_break_the_page(console):
    status, body = get(console, "/api/incidents?limit=banana")
    assert status == 200
    assert len(body) == 3


def test_detail_carries_the_evidence_and_both_states(console):
    _, rows = get(console, "/api/incidents")
    reverted = next(r for r in rows if r["state"] == "reverted")
    _, d = get(console, f"/api/incident/{reverted['id']}")

    assert d["state"] == "reverted"
    assert d["findings"][0]["evidence"]["reason"] == "ImagePullBackOff"
    # before/after is the reversibility argument; a console that showed only the
    # new value would be hiding the half that makes the change undoable.
    action = d["actions"][0]
    assert action["before"] == {"revision": 2}
    assert action["after"] == {"revision": 1}
    assert "still failing" in d["verify_detail"]


@pytest.mark.parametrize("path", ["/api/incident/abc", "/api/incident/9999", "/api/nope"])
def test_bad_paths_are_errors_not_crashes(console, path):
    with pytest.raises(HTTPError) as exc:
        get(console, path)
    assert exc.value.code in (400, 404)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_the_console_refuses_to_be_written_to(console, method):
    """Read-only is the point: nothing here may reach a cluster or a repository."""
    req = Request(console + "/api/incidents", method=method, data=b"{}")  # noqa: S310
    with pytest.raises(HTTPError) as exc:
        urlopen(req, timeout=5)  # noqa: S310
    assert exc.value.code == 405


def test_an_empty_journal_serves_an_empty_page(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(tmp_path / "fresh.db"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert get(base, "/api/incidents")[1] == []
        assert get(base, "/api/summary")[1]["incidents"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_workload_name_alone_still_finds_its_history(console):
    """A name typed without picking a namespace means "in any", not "in none".

    Filtering to nothing here would render as "this workload has no history",
    which is a different and wrong answer.
    """
    _, rows = get(console, "/api/incidents?workload=report-worker")
    assert [r["target"] for r in rows] == ["jobs/deployment/report-worker"]
