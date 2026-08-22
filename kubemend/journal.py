"""An append-only record of what the agent saw, decided, and did.

A single run tells you about one moment. The questions that matter about an
automated system acting on production are all longitudinal:

- Which workload keeps breaking? A service remediated four times this week has a
  problem no rollback is going to fix.
- How often does its own fix fail? That is the agent's accuracy, measured by the
  agent, and it is the number that should decide whether it earns more autonomy.
- What did policy refuse, and how often? A gate that never refuses is not a gate.

None of that is answerable from stdout, so every run is written here: findings,
the plan, the gate's verdict, the commit, and whether verification confirmed
recovery. Refusals and findings with no safe action are recorded too — what the
agent declined to do is as much a part of the audit trail as what it did.

SQLite from the standard library, so the package still has no dependencies and
the log is a single file you can copy, inspect with any tool, or delete.

**Bookkeeping never breaks remediation.** Every write is wrapped: if the journal
is unwritable the run continues and says so. Failing to fix a broken cluster
because a log file was read-only would be a poor trade.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Journal", "JournalError", "IncidentRow", "Stats", "DEFAULT_PATH"]

DEFAULT_PATH = Path.home() / ".kubemend" / "journal.db"

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT    NOT NULL,
    policy      TEXT    NOT NULL DEFAULT '',
    namespace   TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT '',
    dry_run     INTEGER NOT NULL DEFAULT 0,
    findings    INTEGER NOT NULL DEFAULT 0,
    incidents   INTEGER NOT NULL DEFAULT 0,
    commits     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY,
    -- Nullable: an incident recorded outside a run is still worth keeping, and
    -- requiring a run row would mean losing the record rather than the context.
    run_id        INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    created_at    TEXT    NOT NULL,
    namespace     TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    rule          TEXT    NOT NULL DEFAULT '',
    summary       TEXT    NOT NULL DEFAULT '',
    allowed       INTEGER NOT NULL DEFAULT 0,
    autonomy      TEXT    NOT NULL DEFAULT '',
    refusals      TEXT    NOT NULL DEFAULT '',
    commit_sha    TEXT    NOT NULL DEFAULT '',
    branch        TEXT    NOT NULL DEFAULT '',
    pr_url        TEXT    NOT NULL DEFAULT '',
    verification  TEXT    NOT NULL DEFAULT '',
    verify_detail TEXT    NOT NULL DEFAULT '',
    reverted_sha  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    rule        TEXT    NOT NULL,
    severity    TEXT    NOT NULL,
    summary     TEXT    NOT NULL DEFAULT '',
    evidence    TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS actions (
    id             INTEGER PRIMARY KEY,
    incident_id    INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL,
    container      TEXT    NOT NULL DEFAULT '',
    before_state   TEXT    NOT NULL DEFAULT '{}',
    after_state    TEXT    NOT NULL DEFAULT '{}',
    impacted_pods  INTEGER NOT NULL DEFAULT 0,
    written        INTEGER NOT NULL DEFAULT 0,
    detail         TEXT    NOT NULL DEFAULT '',
    skipped_reason TEXT    NOT NULL DEFAULT ''
);

-- The queries this exists to answer are "what happened to this workload" and
-- "what happened lately", so those are the indexes.
CREATE INDEX IF NOT EXISTS idx_incidents_target ON incidents(namespace, kind, name);
CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(created_at DESC);
"""


class JournalError(RuntimeError):
    """The journal could not be opened or written."""


@dataclass
class IncidentRow:
    """One recorded incident, flattened for display."""

    id: int
    created_at: str
    target: str
    rule: str
    summary: str
    allowed: bool
    autonomy: str
    commit_sha: str
    verification: str
    reverted_sha: str

    @property
    def state(self) -> str:
        """A single word for what became of this incident."""
        if self.reverted_sha:
            return "reverted"
        if not self.allowed:
            return "refused"
        if not self.commit_sha:
            return "reported"
        if self.verification == "recovered":
            return "verified"
        if self.verification:
            return self.verification
        return "committed"


@dataclass
class Stats:
    """The numbers worth putting in front of someone deciding on autonomy."""

    runs: int = 0
    incidents: int = 0
    committed: int = 0
    reverted: int = 0
    refused: int = 0
    verified: int = 0

    @property
    def revert_rate(self) -> float:
        """How often the agent's own fix failed to hold.

        The honest measure of whether it deserves more autonomy, and the reason
        the journal records verification outcomes rather than just commits.
        """
        return self.reverted / self.committed if self.committed else 0.0

    @property
    def verified_rate(self) -> float:
        return self.verified / self.committed if self.committed else 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(raw: str):
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:  # pragma: no cover - we wrote it
        return {}


class Journal:
    """A SQLite-backed record of runs. Safe to open concurrently; writes are small."""

    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path = Path(path).expanduser()
        self.available = True
        self.error = ""
        try:
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        except (sqlite3.Error, OSError) as exc:
            # A journal that cannot be opened must not stop a cluster being
            # fixed. Record why and degrade to doing nothing.
            self.available = False
            self.error = str(exc)
            self._conn = None

    # --- writing ----------------------------------------------------------

    @contextmanager
    def _write(self):
        """Run a write, swallowing failures so bookkeeping cannot break a run."""
        conn = self._conn if (self.available and self._conn is not None) else None
        if conn is None:
            yield None
            return
        # The yield comes before anything that can fail, so a broken database
        # cannot leave the caller holding a context manager that never opened.
        try:
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            self.available = False
            self.error = str(exc)
            try:
                conn.rollback()
            except sqlite3.Error:
                pass

    def start_run(self, policy: str, namespace: str, source: str, dry_run: bool = False) -> int:
        with self._write() as conn:
            if conn is None:
                return 0
            cur = conn.execute(
                "INSERT INTO runs (started_at, policy, namespace, source, dry_run)"
                " VALUES (?, ?, ?, ?, ?)",
                (_now(), policy, namespace or "", source, int(dry_run)),
            )
            return int(cur.lastrowid)
        return 0

    def finish_run(self, run_id: int, findings: int, incidents: int, commits: int) -> None:
        with self._write() as conn:
            if conn is None or not run_id:
                return
            conn.execute(
                "UPDATE runs SET findings = ?, incidents = ?, commits = ? WHERE id = ?",
                (findings, incidents, commits, run_id),
            )

    def record(self, run_id: int, plan, verdict, emission=None, verification=None,
               reverted_sha: str = "") -> int:
        """Record one incident: what was seen, decided, written, and confirmed."""
        with self._write() as conn:
            if conn is None:
                return 0
            target = next(iter(plan.targets), None)
            primary = plan.findings[0] if plan.findings else None
            cur = conn.execute(
                "INSERT INTO incidents (run_id, created_at, namespace, kind, name, rule, summary,"
                " allowed, autonomy, refusals, commit_sha, branch, pr_url, verification,"
                " verify_detail, reverted_sha)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id or None, _now(),
                    target.namespace if target else "", target.kind if target else "",
                    target.name if target else "",
                    primary.rule if primary else "", primary.summary if primary else "",
                    int(bool(verdict.allowed)), verdict.autonomy.value,
                    "; ".join(v.code for v in verdict.violations),
                    getattr(emission, "commit", "") or "",
                    getattr(emission, "branch", "") or "",
                    getattr(emission, "pr_url", "") or "",
                    verification.outcome.value if verification else "",
                    verification.explain() if verification else "",
                    reverted_sha,
                ),
            )
            incident_id = int(cur.lastrowid)

            conn.executemany(
                "INSERT INTO findings (incident_id, rule, severity, summary, evidence)"
                " VALUES (?,?,?,?,?)",
                [
                    (incident_id, f.rule, f.severity.value, f.summary, json.dumps(f.evidence))
                    for f in plan.findings
                ],
            )

            written = {id(c.action): c for c in getattr(emission, "changes", [])}
            rows = []
            for action in plan.actions:
                change = written.get(id(action))
                rows.append((
                    incident_id, action.kind.value, action.container,
                    json.dumps(action.before), json.dumps(action.after),
                    action.impacted_pods,
                    int(bool(change and change.applied)),
                    getattr(change, "detail", "") or "",
                    getattr(change, "skipped_reason", "") or "",
                ))
            conn.executemany(
                "INSERT INTO actions (incident_id, kind, container, before_state, after_state,"
                " impacted_pods, written, detail, skipped_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            return incident_id
        return 0

    # --- reading ----------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.available or self._conn is None:
            return []
        try:
            return list(self._conn.execute(sql, params))
        except sqlite3.Error:  # pragma: no cover
            return []

    def recent(self, limit: int = 20, namespace: str = "") -> list[IncidentRow]:
        sql = ("SELECT * FROM incidents"
               + (" WHERE namespace = ?" if namespace else "")
               + " ORDER BY id DESC LIMIT ?")
        params = (namespace, limit) if namespace else (limit,)
        return [self._row(r) for r in self._query(sql, params)]

    def history(self, namespace: str, name: str, limit: int = 50) -> list[IncidentRow]:
        """Everything that has happened to one workload, newest first.

        An empty namespace means "in any" rather than "in the namespace named
        empty string". Filtering to nothing and rendering it as an empty log
        would read as "this workload has no history", which is a different and
        wrong answer.
        """
        sql = ("SELECT * FROM incidents WHERE name = ?"
               + (" AND namespace = ?" if namespace else "")
               + " ORDER BY id DESC LIMIT ?")
        params = (name, namespace, limit) if namespace else (name, limit)
        return [self._row(r) for r in self._query(sql, params)]

    def repeat_offenders(self, limit: int = 10) -> list[tuple[str, int]]:
        """Workloads remediated most often.

        A service that keeps coming back has a problem no rollback will fix, and
        that pattern is invisible from any single run.
        """
        return [
            (f"{r['namespace']}/{r['name']}", int(r["n"]))
            for r in self._query(
                "SELECT namespace, name, COUNT(*) AS n FROM incidents"
                " GROUP BY namespace, name ORDER BY n DESC, name LIMIT ?",
                (limit,),
            )
        ]

    def detail(self, incident_id: int) -> dict:
        """One incident in full: what was seen, what was proposed, what was written.

        The summary views answer "what happened". This answers "on what
        evidence", which is the question anyone reviewing an automated change
        actually has.
        """
        rows = self._query("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if not rows:
            return {}
        r = rows[0]

        findings = []
        for f in self._query(
            "SELECT rule, severity, summary, evidence FROM findings"
            " WHERE incident_id = ? ORDER BY id", (incident_id,)
        ):
            try:
                evidence = json.loads(f["evidence"] or "{}")
            except json.JSONDecodeError:  # pragma: no cover - written by us
                evidence = {}
            findings.append({
                "rule": f["rule"], "severity": f["severity"],
                "summary": f["summary"], "evidence": evidence,
            })

        actions = []
        for a in self._query(
            "SELECT kind, container, before_state, after_state, impacted_pods,"
            " written, detail, skipped_reason FROM actions"
            " WHERE incident_id = ? ORDER BY id", (incident_id,)
        ):
            actions.append({
                "kind": a["kind"], "container": a["container"],
                "before": _loads(a["before_state"]), "after": _loads(a["after_state"]),
                "impacted_pods": int(a["impacted_pods"]),
                "written": bool(a["written"]),
                "detail": a["detail"], "skipped_reason": a["skipped_reason"],
            })

        row = self._row(r)
        return {
            "id": row.id, "created_at": row.created_at, "target": row.target,
            "state": row.state, "rule": row.rule, "summary": row.summary,
            "allowed": row.allowed, "autonomy": row.autonomy,
            "refusals": [c for c in (r["refusals"] or "").split("; ") if c],
            "commit": row.commit_sha, "branch": r["branch"], "pr_url": r["pr_url"],
            "verification": row.verification, "verify_detail": r["verify_detail"],
            "reverted": row.reverted_sha,
            "findings": findings, "actions": actions,
        }

    def record_for(self, namespace: str, name: str) -> "Record":
        """One workload's track record, for deciding what it has earned.

        The streak is counted backwards from the most recent incident and stops
        at the first revert, because promotion is an argument about what has
        happened *since* the agent was last wrong here. A long lifetime record
        with a revert yesterday is not a good record.
        """
        from .earn import Record

        rows = self._query(
            "SELECT commit_sha, reverted_sha, verification FROM incidents"
            " WHERE namespace = ? AND name = ? ORDER BY id DESC",
            (namespace, name),
        )
        committed = verified = reverted = streak = 0
        counting = True
        for r in rows:
            if not r["commit_sha"]:
                continue                       # refused or reported: not a fix
            committed += 1
            if r["reverted_sha"]:
                reverted += 1
                counting = False               # the streak ends here
                continue
            if r["verification"] == "recovered":
                verified += 1
                if counting:
                    streak += 1
            else:
                # Committed but never confirmed. Not a failure, but not
                # evidence either, so it stops the streak without counting
                # against the workload.
                counting = False
        return Record(
            workload=f"{namespace}/{name}",
            committed=committed, verified=verified, reverted=reverted, streak=streak,
        )

    def standings(self, limit: int = 20) -> list["Record"]:
        """Every workload the agent has actually written a fix for.

        Refusals and reports are excluded: a workload the agent only ever talked
        about has no track record, and listing it would imply otherwise.
        """
        rows = self._query(
            "SELECT DISTINCT namespace, name FROM incidents"
            " WHERE commit_sha != '' ORDER BY namespace, name LIMIT ?",
            (limit,),
        )
        return [self.record_for(r["namespace"], r["name"]) for r in rows]

    def namespaces(self) -> list[str]:
        return [r["namespace"] for r in self._query(
            "SELECT DISTINCT namespace FROM incidents ORDER BY namespace"
        )]

    def stats(self) -> Stats:
        rows = self._query(
            "SELECT COUNT(*) AS incidents,"
            " SUM(commit_sha != '') AS committed,"
            " SUM(reverted_sha != '') AS reverted,"
            " SUM(allowed = 0) AS refused,"
            " SUM(verification = 'recovered') AS verified"
            " FROM incidents"
        )
        runs = self._query("SELECT COUNT(*) AS n FROM runs")
        if not rows:
            return Stats()
        r = rows[0]
        return Stats(
            runs=int(runs[0]["n"]) if runs else 0,
            incidents=int(r["incidents"] or 0),
            committed=int(r["committed"] or 0),
            reverted=int(r["reverted"] or 0),
            refused=int(r["refused"] or 0),
            verified=int(r["verified"] or 0),
        )

    @staticmethod
    def _row(r: sqlite3.Row) -> IncidentRow:
        return IncidentRow(
            id=int(r["id"]), created_at=r["created_at"],
            target=f"{r['namespace']}/{r['kind'].lower()}/{r['name']}",
            rule=r["rule"], summary=r["summary"],
            allowed=bool(r["allowed"]), autonomy=r["autonomy"],
            commit_sha=r["commit_sha"], verification=r["verification"],
            reverted_sha=r["reverted_sha"],
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
