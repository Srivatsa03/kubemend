"""Command line: observe a cluster, diagnose it, and show what policy permits.

The output is built around one distinction that most tooling collapses: what the
agent *concluded* and what it is *allowed to do about it* are separate, and both
are shown. A finding with no permitted action is not a failure of the system,
it is the system working.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from .gitops import GitError, GitOpsRepo
from .journal import DEFAULT_PATH, Journal
from .model import Autonomy, Severity
from .plan import propose, unaddressed
from .safety import CONSERVATIVE, STAGING, gate
from .signals import detect
from .verify import Outcome, verify

POLICIES = {"conservative": CONSERVATIVE, "staging": STAGING}

# kubectl resources the snapshot is built from. Read-only, and the whole set is
# listed here so it is obvious exactly what the agent looks at.
RESOURCES = {"pods": "pods", "deployments": "deployments", "events": "events"}


def collect(namespace: str | None = None, context: str | None = None) -> dict:
    """Build a snapshot by shelling out to kubectl with read-only verbs."""
    snapshot = {}
    for key, resource in RESOURCES.items():
        cmd = ["kubectl", "get", resource, "-o", "json"]
        cmd += ["-n", namespace] if namespace else ["--all-namespaces"]
        if context:
            cmd += ["--context", context]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        except FileNotFoundError:
            raise SystemExit("kubectl not found on PATH; use --snapshot to read recorded state")
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"kubectl get {resource} failed: {exc.stderr.strip()}")
        snapshot[key] = json.loads(out.stdout)
    return snapshot


def _color(enabled: bool):
    def paint(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if enabled else text
    return paint


SEV_COLOR = {Severity.CRITICAL: "31;1", Severity.WARNING: "33", Severity.INFO: "36"}
AUTONOMY_COLOR = {Autonomy.APPLY: "32;1", Autonomy.PROPOSE: "33", Autonomy.REPORT: "2"}


def render(findings, decisions, orphans, policy_name: str, color: bool) -> str:
    """Render findings, then each incident with the verdict that governs it."""
    c = _color(color)
    DIM, BOLD = "2", "1"
    lines = [
        c(BOLD, "\n  kubemend")
        + c(DIM, f"   {len(findings)} finding(s), {len(decisions)} incident(s), policy '{policy_name}'\n")
    ]

    for f in findings:
        badge = c(SEV_COLOR[f.severity], f"{f.severity.value:>8}")
        lines.append(f"  {badge}  {str(f.target):<34} {c(DIM, f.rule)}")
        lines.append(f"            {f.summary}")

    lines.append(c(BOLD, "\n  remediation") + c(DIM, "   one plan per incident, gated independently\n"))

    for plan, verdict in decisions:
        mark = "✓" if verdict.allowed else "✗"
        tone = AUTONOMY_COLOR[verdict.autonomy] if verdict.allowed else "31;1"
        label = verdict.autonomy.value.upper() if verdict.allowed else "REFUSED"
        target = next(iter(plan.targets))
        lines.append(f"    {c(tone, mark)} {c(tone, f'{label:<8}')} {str(target)}")
        for a in plan.actions:
            lines.append(f"        {a.describe()}")
            lines.append(f"        {c(DIM, '↳ ' + a.reason)}")
            if a.reversible:
                lines.append(f"        {c(DIM, 'undo: ' + a.inverse().describe())}")
        for v in verdict.violations:
            lines.append(f"        {c(DIM, '· [' + v.code + '] ' + v.message)}")
            if v.remedy:
                lines.append(f"          {c(DIM, 'remedy: ' + v.remedy)}")
        lines.append("")

    if orphans:
        targets = sorted({str(f.target) for f in orphans})
        lines.append(c(BOLD, "  no automated fix") + c(DIM, "   reported for a human\n"))
        for t in targets:
            reasons = sorted({f.rule for f in orphans if str(f.target) == t})
            lines.append(f"    {c(DIM, '·')} {t:<34} {c(DIM, ', '.join(reasons))}")
        lines.append("")
    return "\n".join(lines)


def remediate(args) -> int:
    """Diagnose, gate, and write the permitted plans to a GitOps repository.

    Policy still decides everything: a plan refused by the gate is never
    rendered, and a plan held at 'propose' becomes a branch rather than a commit
    on the mainline. --dry-run overrides all of it downward, never upward.
    """
    if args.snapshot:
        with open(args.snapshot, encoding="utf-8") as fh:
            snapshot = json.load(fh)
    else:
        snapshot = collect(args.namespace, args.context)

    try:
        repo = GitOpsRepo(args.repo)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    findings = detect(snapshot)
    plans = propose(findings, snapshot)
    policy = POLICIES[args.policy]
    use_color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    c = _color(use_color)
    DIM, BOLD = "2", "1"

    journal = Journal(args.journal) if args.journal != "none" else None
    run_id = journal.start_run(
        args.policy, args.namespace or "", "snapshot" if args.snapshot else "live", args.dry_run
    ) if journal else 0

    print(c(BOLD, f"\n  kubemend remediate") +
          c(DIM, f"   {len(plans)} incident(s), policy '{args.policy}', repo {repo.path.name}\n"))

    written = 0
    for plan in plans:
        target = next(iter(plan.targets))
        # The workload's own record, if there is a log to read it from. Passed
        # in rather than looked up inside the gate, so the gate stays a function
        # of its arguments.
        record = (journal.record_for(target.namespace, target.name)
                  if journal and journal.available and not args.ignore_history else None)
        verdict = gate(plan, policy, recent_plans=args.recent_plans, record=record)
        if not verdict.allowed:
            print(f"    {c('31;1', '✗')} {c('31;1', 'REFUSED ')} {target}")
            for v in verdict.violations:
                print(f"        {c(DIM, '· ' + v.message)}")
            if journal:
                journal.record(run_id, plan, verdict)
            print()
            continue

        autonomy = Autonomy.REPORT if args.dry_run else verdict.autonomy
        result = None
        reverted = ""
        try:
            emission = repo.emit(plan, autonomy, push=not args.no_push)
        except GitError as exc:
            print(f"    {c('31;1', '✗')} {c('31;1', 'ERROR   ')} {target}\n        {exc}\n",
                  file=sys.stderr)
            return 2

        earned = next(
            (v for v in verdict.violations if v.code.startswith("earned_")), None
        )
        label = "DRY RUN" if args.dry_run else autonomy.value.upper()
        tone = AUTONOMY_COLOR[autonomy]
        print(f"    {c(tone, '✓')} {c(tone, f'{label:<8}')} {target}")
        if earned:
            arrow = "↑" if earned.code == "earned_promotion" else "↓"
            print(f"        {c('36', arrow + ' ' + earned.message)}")
        for change in emission.applied:
            print(f"        {change.detail}")
        for change in emission.skipped:
            print(f"        {c(DIM, '· not written: ' + change.skipped_reason)}")
        if emission.committed:
            written += 1
            print(f"        {c(DIM, f'commit {emission.commit[:8]} on {emission.branch}')}")
            if emission.pushed:
                print(f"        {c(DIM, 'pushed to origin/' + emission.branch)}")
            elif emission.push_note:
                # Loud, because an undelivered commit is not a fix. Whatever is
                # reconciling the cluster is still looking at the old state.
                print(f"        {c('33', emission.push_note)}")
            if args.pr and autonomy is Autonomy.PROPOSE:
                url = repo.open_pull_request(emission)
                print(f"        {c(DIM, url or emission.pr_note)}")
        elif emission.applied:
            print(f"        {c(DIM, 'rendered only; nothing committed')}")
        if emission.diff:
            for line in emission.diff.splitlines():
                if line.startswith(("+++", "---", "diff ", "index ", "@@")):
                    continue
                if line.startswith("+"):
                    print("        " + c("32;1", line))
                elif line.startswith("-"):
                    print("        " + c("31;1", line))

        # Verification only means anything for a change that actually reached
        # the mainline *and* the remote. A branch awaiting review has not been
        # applied to anything yet, and a commit that failed to push has not
        # either — watching one would time out against a cluster that was never
        # sent the fix, and then revert a change that might have worked.
        if (args.verify and autonomy is Autonomy.APPLY
                and emission.committed and not emission.delivered):
            print(f"        {c('33', 'not verifying: the change was never delivered')}")
        elif args.verify and emission.delivered and autonomy is Autonomy.APPLY:
            print(f"        {c(DIM, 'watching for recovery...')}")
            def read_cluster():
                # collect() exits the process on a kubectl failure, which is
                # right for a one-shot command and wrong here: losing the API
                # server mid-watch is exactly the case verification is meant to
                # report as indeterminate rather than die on.
                try:
                    return collect(args.namespace, args.context)
                except SystemExit as exc:
                    raise RuntimeError(str(exc)) from exc

            result = verify(
                target,
                read_cluster,
                motivating=plan.findings,
                timeout=args.verify_timeout,
            )
            tone2 = "32;1" if result.healthy else "31;1"
            print(f"        {c(tone2, result.explain())}")
            if result.outcome.should_revert:
                reverted = repo.revert(emission.commit, result.explain(),
                                       push=not args.no_push)
                written -= 1
                print(f"        {c('33', f'reverted in {reverted[:8]}')}")
        if journal:
            journal.record(run_id, plan, verdict, emission, result, reverted)
        print()

    orphans = unaddressed(findings, plans)
    if orphans:
        print(c(BOLD, "  no automated fix") + c(DIM, "   reported for a human\n"))
        for t_ in sorted({str(f.target) for f in orphans}):
            print(f"    {c(DIM, '· ' + t_)}")
        print()

    if journal:
        journal.finish_run(run_id, len(findings), len(plans), written)
        if not journal.available:
            print(c(DIM, f"  (incident log unavailable: {journal.error})"))
        journal.close()

    print(c(DIM, f"  {written} commit(s) written.\n"))
    return 0


STATE_COLOR = {
    "verified": "32;1", "committed": "36", "reported": "2",
    "refused": "31;1", "reverted": "33", "indeterminate": "33", "still_failing": "31;1",
}


def show_log(args) -> int:
    """Read back the incident log.

    Every run answers "what is wrong now". This answers the questions that only
    a history can: which workload keeps breaking, and how often the agent's own
    fix failed to hold.
    """
    journal = Journal(args.journal)
    if not journal.available:
        print(f"error: {journal.error}", file=sys.stderr)
        return 2

    use_color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    c = _color(use_color)
    DIM, BOLD = "2", "1"

    filtered = bool(args.workload or args.namespace)
    rows = (journal.history(args.namespace, args.workload, args.limit) if args.workload
            else journal.recent(args.limit, args.namespace))

    scope = ""
    if args.workload:
        scope = f"   {args.namespace + '/' if args.namespace else ''}{args.workload}"
    elif args.namespace:
        scope = f"   namespace {args.namespace}"

    print(c(BOLD, "\n  incident log") + c(DIM, f"{scope or '   ' + str(journal.path)}\n"))
    if not rows:
        print(c(DIM, "  nothing recorded here yet\n"))
        journal.close()
        return 0

    for row in rows:
        tone = STATE_COLOR.get(row.state, "2")
        when = row.created_at.replace("T", " ").rsplit("+", 1)[0]
        print(f"  {c(DIM, when)}  {c(tone, f'{row.state:<13}')} {row.target}")
        if row.summary:
            print(f"      {c(DIM, row.summary[:96])}")

    # The rows above respect the filter; the totals below never do. Saying so
    # is cheaper than a scoped aggregate nobody asked for, and a mislabelled
    # revert rate is exactly the kind of number that gets quoted back at you.
    stats = journal.stats()
    across = f"   across {stats.runs} run(s)" + (", all workloads" if filtered else "")
    print(c(BOLD, "\n  totals") + c(DIM, f"{across}\n"))
    print(f"    incidents   {stats.incidents}")
    print(f"    committed   {stats.committed}")
    print(f"    refused     {stats.refused}")
    print(f"    verified    {stats.verified}")
    # The agent's own accuracy, which is what should decide whether it earns
    # more autonomy.
    if stats.committed:
        print(f"    {c(BOLD, 'revert rate')} {stats.revert_rate:.0%}  "
              f"{c(DIM, f'({stats.reverted} of {stats.committed} fixes did not hold)')}")

    # What each workload has earned. This is the log's reason for existing made
    # visible: the numbers above are only worth collecting if something acts on
    # them, and this is the something.
    standings = [] if args.workload else journal.standings()
    if standings:
        from .earn import adjust
        from .model import Autonomy
        print(c(BOLD, "\n  standing") +
              c(DIM, "   what each workload has earned on its own record\n"))
        for rec in sorted(standings, key=lambda r: (-r.streak, r.workload)):
            moved = adjust(Autonomy.PROPOSE, rec, ceiling=Autonomy.APPLY)
            tone = "32;1" if moved.promoted else ("33" if moved.demoted else DIM)
            mark = "↑" if moved.promoted else ("↓" if moved.demoted else "·")
            print(f"    {c(tone, mark)} {rec.workload:<34} "
                  f"{c(DIM, f'{rec.verified}/{rec.committed} held, streak {rec.streak}')}")
            print(f"        {c(DIM, moved.reason)}")

    # Pointless when the caller already named the workload they care about.
    repeats = [] if args.workload else [
        (w, n) for w, n in journal.repeat_offenders(5) if n > 1
    ]
    if repeats:
        print(c(BOLD, "\n  keeps coming back") +
              c(DIM, "   a rollback is not going to fix these\n"))
        for workload, n in repeats:
            print(f"    {n:>3}x  {workload}")
    print()
    journal.close()
    return 0


def serve_log(args) -> int:
    """Serve the console. Read-only, and localhost unless told otherwise."""
    from .serve import serve

    # Opened once to fail fast on an unreadable path; the server opens its own
    # per request, since its threads cannot share this one.
    journal = Journal(args.journal)
    available, error, path = journal.available, journal.error, journal.path
    journal.close()
    if not available:
        print(f"error: {error}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{args.port}"
    print(f"\n  kubemend console   {url}")
    print(f"  reading            {path}")
    if args.host not in ("127.0.0.1", "localhost"):
        # Worth saying out loud: this file is an inventory of what breaks.
        print(f"  \033[33mserving on {args.host}; the log names your workloads\033[0m")
    print("  read-only; ctrl-c to stop\n")

    if args.open:
        import webbrowser

        webbrowser.open(url)
    try:
        serve(path, args.host, args.port)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port} ({exc})", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kubemend",
        description="Diagnose a Kubernetes cluster and show what remediation policy allows.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("diagnose", help="find problems and propose a bounded plan")
    src = diag.add_mutually_exclusive_group()
    src.add_argument("--snapshot", help="read recorded cluster JSON instead of a live cluster")
    src.add_argument("--context", help="kubectl context to read from")
    diag.add_argument("-n", "--namespace", help="limit to one namespace (default: all)")
    diag.add_argument(
        "--policy", choices=list(POLICIES), default="conservative",
        help="which policy governs the plan (default: conservative)",
    )
    diag.add_argument(
        "--recent-plans", type=int, default=0,
        help="plans already applied this window, for flap protection",
    )
    diag.add_argument("--json", dest="json_out", help="write the full result as JSON")
    diag.add_argument("--no-color", action="store_true")
    diag.add_argument(
        "--fail-on", choices=[s.value for s in Severity],
        help="exit non-zero if a finding at or above this severity exists",
    )

    rem = sub.add_parser(
        "remediate",
        help="write the permitted plans to a GitOps repository as commits",
    )
    src2 = rem.add_mutually_exclusive_group()
    src2.add_argument("--snapshot", help="read recorded cluster JSON instead of a live cluster")
    src2.add_argument("--context", help="kubectl context to read from")
    rem.add_argument("--repo", required=True, help="path to the GitOps repository checkout")
    rem.add_argument("-n", "--namespace", help="limit to one namespace (default: all)")
    rem.add_argument(
        "--policy", choices=list(POLICIES), default="conservative",
        help="which policy governs the plans (default: conservative)",
    )
    rem.add_argument(
        "--recent-plans", type=int, default=0,
        help="plans already applied this window, for flap protection",
    )
    rem.add_argument(
        "--dry-run", action="store_true",
        help="render the diffs and revert, committing nothing whatever policy allows",
    )
    rem.add_argument(
        "--verify", action="store_true",
        help="after applying, watch the workload and revert the commit if it does not recover",
    )
    rem.add_argument(
        "--verify-timeout", type=float, default=180.0,
        help="how long to wait for recovery before giving up (default: 180s)",
    )
    rem.add_argument(
        "--pr", action="store_true",
        help="push proposed branches and open a pull request (needs a remote and gh)",
    )
    rem.add_argument(
        "--ignore-history", action="store_true",
        help="gate on policy alone, ignoring what this workload has earned",
    )
    rem.add_argument(
        "--no-push", action="store_true",
        help="commit without pushing; the reconciler will not see the change",
    )
    rem.add_argument(
        "--journal", default=str(DEFAULT_PATH),
        help=f"incident log to append to (default: {DEFAULT_PATH}); 'none' to disable",
    )
    rem.add_argument("--no-color", action="store_true")

    snap = sub.add_parser("snapshot", help="record cluster state to a file for offline analysis")
    snap.add_argument("--out", default="snapshot.json")
    snap.add_argument("-n", "--namespace")
    snap.add_argument("--context")

    sub.add_parser("policy", help="print the shipped policies")

    srv = sub.add_parser("serve", help="browse the incident log in a browser")
    srv.add_argument("--journal", default=str(DEFAULT_PATH))
    srv.add_argument("--port", type=int, default=8420)
    srv.add_argument(
        "--host", default="127.0.0.1",
        help="the log names what is fragile in your cluster; bind elsewhere deliberately",
    )
    srv.add_argument("--open", action="store_true", help="open a browser window")

    log = sub.add_parser("log", help="read the incident log")
    log.add_argument("--journal", default=str(DEFAULT_PATH))
    log.add_argument("-n", "--namespace", default="", help="limit to one namespace")
    log.add_argument("--workload", default="", help="history for one workload, as name")
    log.add_argument("--limit", type=int, default=20)
    log.add_argument("--no-color", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "policy":
        for name, policy in POLICIES.items():
            print(f"\n{name}")
            print(f"  allowed actions   {', '.join(sorted(k.value for k in policy.allowed_kinds)) or 'none'}")
            for kind in sorted(policy.allowed_kinds, key=lambda k: k.value):
                print(f"    {kind.value:<16} -> {policy.ceiling(kind).value}")
            print(f"  max pods          {policy.max_impacted_pods}")
            print(f"  max workloads     {policy.max_workloads}")
            print(f"  max namespaces    {policy.max_namespaces}")
            print(f"  reversible only   {policy.require_reversible}")
            print(f"  protected         {len(policy.protected_namespaces)} namespaces")
        print()
        return 0

    if args.command == "snapshot":
        snapshot = collect(args.namespace, args.context)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)
        counts = {k: len((v or {}).get("items", [])) for k, v in snapshot.items()}
        print(f"wrote {args.out}: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
        return 0

    if args.command == "remediate":
        return remediate(args)

    if args.command == "log":
        return show_log(args)

    if args.command == "serve":
        return serve_log(args)

    if args.snapshot:
        with open(args.snapshot, encoding="utf-8") as fh:
            snapshot = json.load(fh)
    else:
        snapshot = collect(args.namespace, args.context)

    findings = detect(snapshot)
    plans = propose(findings, snapshot)
    policy = POLICIES[args.policy]
    decisions = [(p, gate(p, policy, recent_plans=args.recent_plans)) for p in plans]
    orphans = unaddressed(findings, plans)

    use_color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    print(render(findings, decisions, orphans, args.policy, use_color))

    if args.json_out:
        payload = {
            "policy": args.policy,
            "findings": [
                {
                    "rule": f.rule, "severity": f.severity.value, "target": str(f.target),
                    "summary": f.summary, "evidence": f.evidence,
                    "affected_pods": f.affected_pods,
                    "suggests": [k.value for k in f.suggests],
                }
                for f in findings
            ],
            "incidents": [
                {
                    "target": str(next(iter(plan.targets))),
                    "rationale": plan.rationale,
                    "impacted_pods": plan.impacted_pods,
                    "reversible": plan.reversible,
                    "actions": [
                        {
                            "kind": a.kind.value, "target": str(a.target),
                            "before": a.before, "after": a.after, "reason": a.reason,
                            "impacted_pods": a.impacted_pods,
                            "undo": a.inverse().describe() if a.reversible else None,
                        }
                        for a in plan.actions
                    ],
                    "verdict": {
                        "allowed": verdict.allowed,
                        "autonomy": verdict.autonomy.value,
                        "explanation": verdict.explain(),
                        "violations": [
                            {"code": v.code, "message": v.message,
                             "action": v.action, "remedy": v.remedy}
                            for v in verdict.violations
                        ],
                    },
                }
                for plan, verdict in decisions
            ],
            "unaddressed": sorted({str(f.target) for f in orphans}),
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    if args.fail_on:
        from .model import Severity as S
        order = {S.INFO: 0, S.WARNING: 1, S.CRITICAL: 2}
        threshold = order[S(args.fail_on)]
        if any(order[f.severity] >= threshold for f in findings):
            print(f"FAIL: finding at or above '{args.fail_on}' severity", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
