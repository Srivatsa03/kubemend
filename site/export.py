#!/usr/bin/env python3
"""Export everything the site displays, straight from the code and the transcript.

The published page must never be able to drift from what is in this repository,
so nothing on it is typed by hand. The gate verdicts are computed by calling the
real gate, the findings come from the real detector, and the live-cluster
numbers are parsed out of the committed transcript rather than remembered.

If a number on the site looks wrong, the fix is in the code, not in the HTML.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kubemend.model import ActionKind, Autonomy  # noqa: E402
from kubemend.plan import propose  # noqa: E402
from kubemend.safety import (  # noqa: E402
    CONSERVATIVE, CONTROL_PLANE_NAMESPACES, STAGING, gate,
)
from kubemend.signals import detect  # noqa: E402

FIXTURE = ROOT / "fixtures" / "broken-cluster.json"
TRANSCRIPT = ROOT / "demo" / "transcript.txt"

# Kept beside the rules so the site says what each one catches in one line.
RULE_BLURB = {
    "crashloop": "Container the kubelet has given up restarting promptly",
    "oomkilled": "Killed by the kernel for exceeding its memory limit",
    "image_pull": "Bad tag, bad registry, or missing pull credentials",
    "config_error": "References a ConfigMap or Secret that does not exist",
    "rollout_stuck": "Rollout past its progress deadline",
    "replica_shortfall": "Fewer replicas available than desired",
    "unschedulable": "No node can fit the pod",
    "flapping": "Restarting repeatedly while still reporting healthy",
}

ACTION_BLURB = {
    "scale": ("Change replica count", "spec.replicas"),
    "rollback": ("Revert a workload to a prior revision", "restored from git history"),
    "restart": ("Trigger a rolling restart", "no manifest field — refused"),
    "set_resources": ("Adjust requests or limits", "…containers.c.resources"),
    "set_image": ("Pin or correct a container image", "…containers.c.image"),
    "set_probe": ("Adjust probe timings or thresholds", "no manifest field — refused"),
}


def target_of(plan):
    t = next(iter(plan.targets))
    return f"{t.namespace}/{t.name}", t.namespace


def analysis() -> dict:
    snapshot = json.loads(FIXTURE.read_text())
    findings = detect(snapshot)
    plans = propose(findings, snapshot)

    in_plan = sum(len(p.findings) for p in plans)
    rows = []
    for plan in sorted(plans, key=lambda p: target_of(p)[0]):
        name, namespace = target_of(plan)
        action = plan.actions[0]
        entry = {
            "workload": name,
            "namespace": namespace,
            "action": action.kind.value,
            "before": action.before,
            "after": action.after,
            "pods": plan.impacted_pods,
            "why": [f.summary for f in plan.findings],
            "rules": sorted({f.rule for f in plan.findings}),
            "verdicts": {},
        }
        for label, policy in (("conservative", CONSERVATIVE), ("staging", STAGING)):
            verdict = gate(plan, policy)
            entry["verdicts"][label] = {
                "allowed": verdict.allowed,
                "autonomy": verdict.autonomy.value,
                "codes": [v.code for v in verdict.violations],
                "reasons": [v.message for v in verdict.violations],
            }
        rows.append(entry)

    counts = {}
    for f in findings:
        counts[f.rule] = counts.get(f.rule, 0) + 1

    return {
        "findings": len(findings),
        "plans": len(plans),
        "findings_in_plans": in_plan,
        "findings_no_action": len(findings) - in_plan,
        "by_rule": [
            {"rule": r, "count": n, "blurb": RULE_BLURB.get(r, "")}
            for r, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "rows": rows,
    }


def policies() -> dict:
    out = {}
    for label, p in (("conservative", CONSERVATIVE), ("staging", STAGING)):
        out[label] = {
            "max_impacted_pods": p.max_impacted_pods,
            "max_workloads": p.max_workloads,
            "max_namespaces": p.max_namespaces,
            "max_plans_per_window": p.max_plans_per_window,
            "default_autonomy": p.default_autonomy.value,
            "autonomy": {k.value: v.value for k, v in sorted(
                p.autonomy.items(), key=lambda kv: kv[0].value)},
            "allowed": sorted(k.value for k in p.allowed_kinds),
        }
    return out


def demo() -> dict:
    """Parse the committed transcript so the live numbers cannot be misquoted."""
    text = TRANSCRIPT.read_text()

    recovered = re.search(r"recovered after (\d+)s", text)
    failed = re.search(r"still failing after (\d+)s: (.+)", text)
    kept = re.search(r"commit ([0-9a-f]{8}) on main\n\s+-\s+(.*)\n\s+\+\s+(.*)", text)
    reverted = re.search(r"reverted in ([0-9a-f]{8})", text)
    rate = re.search(r"revert rate (\d+)%\s+\((\d+) of (\d+)", text)
    offender = re.search(r"(\d+)x\s+(\S+)", text.split("keeps coming back")[-1]) \
        if "keeps coming back" in text else None

    message = ""
    block = re.search(r"^(\s*)(rollback payments/deployment/checkout\n.*?"
                      r"Undo: git revert this commit\.)", text, re.S | re.M)
    if block:
        # The transcript indents the whole commit body; strip the common prefix
        # rather than a guessed width, so the message reads as git stored it.
        lines = (block.group(1) + block.group(2)).splitlines()
        pad = min((len(x) - len(x.lstrip()) for x in lines if x.strip()), default=0)
        message = "\n".join(x[pad:] if len(x) >= pad else x for x in lines).strip()

    return {
        "recovered_seconds": int(recovered.group(1)) if recovered else None,
        "failed_seconds": int(failed.group(1)) if failed else None,
        "failed_reason": failed.group(2).strip() if failed else "",
        "kept_commit": kept.group(1) if kept else "",
        "diff_removed": kept.group(2).strip() if kept else "",
        "diff_added": kept.group(3).strip() if kept else "",
        "reverted_commit": reverted.group(1) if reverted else "",
        "revert_rate": int(rate.group(1)) if rate else None,
        "reverted_of": [int(rate.group(2)), int(rate.group(3))] if rate else None,
        "repeat_offender": {"workload": offender.group(2), "count": int(offender.group(1))}
                           if offender else None,
        "commit_message": message,
    }


def project() -> dict:
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    found = re.search(r"(\d+) tests? collected", tests.stdout)
    loc = sum(len(p.read_text().splitlines()) for p in (ROOT / "kubemend").glob("*.py"))

    return {
        "tests": int(found.group(1)) if found else None,
        "loc": loc,
        "actions": [
            {"kind": k.value, "effect": ACTION_BLURB[k.value][0],
             "field": ACTION_BLURB[k.value][1]}
            for k in ActionKind
        ],
        "autonomy_levels": [a.value for a in Autonomy],
        "protected_namespaces": sorted(CONTROL_PLANE_NAMESPACES),
        "repo": "https://github.com/Srivatsa03/kubemend",
    }


def main() -> None:
    data = {
        "analysis": analysis(),
        "policies": policies(),
        "demo": demo(),
        "project": project(),
    }
    out = Path(__file__).resolve().parent / "data.json"
    out.write_text(json.dumps(data, indent=1) + "\n")

    a, d, p = data["analysis"], data["demo"], data["project"]
    print(f"wrote {out.relative_to(ROOT)}")
    print(f"  {a['findings']} findings -> {a['plans']} plans, "
          f"{a['findings_no_action']} produce no action")
    print(f"  live: recovered {d['recovered_seconds']}s, "
          f"failed {d['failed_seconds']}s, revert rate {d['revert_rate']}%")
    print(f"  {p['tests']} tests, {p['loc']} LOC")


if __name__ == "__main__":
    main()
