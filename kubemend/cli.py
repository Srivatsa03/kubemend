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

from .model import Autonomy, Severity
from .plan import propose, unaddressed
from .safety import CONSERVATIVE, STAGING, gate
from .signals import detect

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

    snap = sub.add_parser("snapshot", help="record cluster state to a file for offline analysis")
    snap.add_argument("--out", default="snapshot.json")
    snap.add_argument("-n", "--namespace")
    snap.add_argument("--context")

    sub.add_parser("policy", help="print the shipped policies")
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
