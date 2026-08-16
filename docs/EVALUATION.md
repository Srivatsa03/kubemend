# Evaluation

What has actually been measured, how, and what the numbers do and do not support.

Two things are worth saying before any of them. First, **the interesting measurement here is not detection accuracy** — detecting a `CrashLoopBackOff` is not hard, and a tool that led with its detection rate would be answering a question nobody is stuck on. The interesting measurement is what happens after the agent acts. Second, every number below is reproducible from this repository with the command printed beside it, and re-derived rather than recalled.

## Summary

| What | Result | Where |
|---|---|---|
| Findings on the recorded fixture | 13 findings → 4 plans | [below](#offline-the-recorded-fixture) |
| Findings producing no action | **5 of 13** | [below](#abstention-is-the-common-case) |
| Gate outcomes, conservative policy | 2 apply, 1 propose, 1 refused | [below](#what-the-gate-did) |
| Live fix that worked | `recovered` after **26s**, commit kept | [below](#live-a-real-k3d-cluster) |
| Live fix that did not | `still_failing` after **76s**, commit reverted | [below](#live-a-real-k3d-cluster) |
| Measured revert rate | **50%** (1 of 2) | [below](#the-number-that-matters) |
| Test suite | 157 tests, 0 runtime dependencies | [below](#test-surface) |

## Offline: the recorded fixture

The repository ships `fixtures/broken-cluster.json`, a recorded snapshot of a cluster having a bad afternoon. The whole analysis path — detection, planning, gating — runs against it with nothing installed and no cluster present.

```bash
kubemend diagnose --snapshot fixtures/broken-cluster.json
```

```
  kubemend   13 finding(s), 4 incident(s), policy 'conservative'
```

### What was detected

| Rule | Count |
|---|---|
| `replica_shortfall` | 4 |
| `crashloop` | 3 |
| `config_error` | 1 |
| `image_pull` | 1 |
| `oomkilled` | 1 |
| `rollout_stuck` | 1 |
| `unschedulable` | 1 |
| `flapping` | 1 |
| **Total** | **13** |

### Abstention is the common case

Thirteen findings become **four** plans. Eight findings are attached to a plan; **five produce no action at all.**

That is a feature being measured, not a coverage gap. A missing ConfigMap needs a value the agent has no business inventing. An unschedulable pod is a capacity decision. A container restarting for unclear reasons needs a human to read the logs. Reporting these clearly and stopping is the correct behaviour, and the test suite asserts it rather than merely permitting it.

> An earlier version of the README claimed 10 of 13. That was wrong, and was corrected on 2026-08-16 when the number was re-derived for the technical report. The correct figure is 5.

Plans are grouped per workload, so several findings can motivate one plan:

| Workload | Findings that motivated it | Action |
|---|---|---|
| `payments/checkout` | `crashloop`, `crashloop`, `rollout_stuck` | `rollback` |
| `payments/api` | `oomkilled` | `set_resources` |
| `jobs/report-worker` | `image_pull`, `replica_shortfall` | `rollback` |
| `kube-system/coredns` | `crashloop`, `replica_shortfall` | `rollback` |

### What the gate did

The same four plans, under the two shipped policies. The gate is a pure function of (plan, policy), so this table is deterministic.

| Workload | Action | Conservative | Staging |
|---|---|---|---|
| `payments/checkout` | `rollback` | **apply** | apply |
| `jobs/report-worker` | `rollback` | **apply** | apply |
| `payments/api` | `set_resources` | **propose** — `autonomy_ceiling` | apply |
| `kube-system/coredns` | `rollback` | **refused** — `protected_namespace` | **refused** — `protected_namespace` |

Two results are worth drawing out.

**The identical action is applied on one workload and refused on another, purely on namespace.** `rollback` on `payments/checkout` proceeds; `rollback` on `kube-system/coredns` is refused. That is the protected-namespace rule doing exactly what it exists to do, and it is a hard refusal — no autonomy level overrides it, which is why the staging policy refuses it too.

**Loosening policy changes destination, not permission.** Moving from conservative to staging promotes `payments/api` from `propose` to `apply`; it does not unlock the control plane. The refusal is structural rather than a threshold.

Reproduce:

```bash
kubemend diagnose --snapshot fixtures/broken-cluster.json
kubemend policy          # what each shipped policy permits
```

## Live: a real k3d cluster

`demo/run.sh` stands up a k3d cluster and a git repository holding its manifests, applies the repository the way Argo CD or Flux would, ships a release that breaks, and lets kubemend read the cluster and write the fix back. The committed transcript in [`../demo/transcript.txt`](../demo/transcript.txt) is the verbatim output of a real run.

**Method.** `kubectl apply` stands in for the reconciler, on a 12-second delay, to model the lag between a commit landing and a reconciler picking it up. The agent is given no special treatment: it reads the live cluster with the same read-only path used against any cluster, and its only write is a git commit. Nothing in the script is scripted around it.

The demo proves **both** terminal outcomes, which matters more for an unattended system than proving the happy path twice.

### Run of 2026-08-15

| Scenario | Setup | Outcome | Time to verdict | Result |
|---|---|---|---|---|
| One bad release | image tag typo | `recovered` | **26s** | commit kept |
| Two bad releases | two bad tags in a row, so rolling back one is not enough | `still_failing` | **76s** | commit reverted automatically |

**The fix that worked.**

```
      ✓ APPLY    payments/deployment/checkout
          restored clusters/prod/checkout.yaml to f14c37f1
          commit 9465b30a on main
          -          image: nginx:1.27-alpine-typo   # known good
          +          image: nginx:1.27-alpine   # known good
          watching for recovery...
          recovered after 26s
    1 commit(s) written.
```

**The fix that did not.** The second scenario ships two bad releases in a row, so the rollback lands on a revision that is *also* broken — the agent's plan is reasonable and still wrong, which is the case that matters.

```
      ✓ APPLY    payments/deployment/checkout
          restored clusters/prod/checkout.yaml to 92a6903d
          commit 9b07e933 on main
          watching for recovery...
          still failing after 76s: cannot pull image nginx:1.27-broken-a
          reverted in 0b0d81f4
    0 commit(s) written.

   c2b3faa Revert "rollback payments/deployment/checkout"
   9b07e93 rollback payments/deployment/checkout
   9d637fb bad release B
```

The repository ends the run in the state a human last approved.

**On the timings.** 26s and 76s are wall-clock from commit to verdict and include the 12s reconciler delay, a 15s settle period, and image-pull time. They vary between runs with cluster scheduling — an earlier run on the same script gave 46s and 75s. What does not vary is the pair of outcomes and which commit survived. Quote the transcript, not a remembered number.

Reproduce (needs `k3d`, `kubectl`, and a running Docker daemon):

```bash
demo/run.sh
```

CI runs this same script against a real cluster on every push. A project whose claim is "it works against a real cluster" should not prove it with mocks.

## The number that matters

Both scenarios above are single anecdotes. The incident log turns them into the measurement this project is actually arguing about.

```
    incidents   2
    committed   2
    refused     0
    verified    1
    revert rate 50%  (1 of 2 fixes did not hold)

  keeps coming back   a rollback is not going to fix these
      2x  payments/checkout
```

**Revert rate is the agent grading itself on real outcomes** — how often its own fix failed to hold and it had to withdraw its own commit. It is the only honest input to "should this be allowed to `apply` rather than `propose`?", and most tooling in this space does not report it, because reporting it means admitting the agent is sometimes wrong.

### What 50% does and does not mean

**It does not mean kubemend fixes half of what it touches.** The demo is deliberately constructed so that one of its two scenarios is unfixable by rollback. The number demonstrates that the measurement works end to end — that a failed fix is detected, withdrawn, and counted — and nothing more.

**A production revert rate is not claimed here, because it has not been measured.** That figure requires a real cluster with real incidents over real time, and this project does not have one yet. Anyone quoting 50% as kubemend's accuracy is quoting a demo parameter.

The repeat-offender line is the same story from the other side: `payments/checkout` appears twice because the demo broke it twice. On a real cluster that column is what distinguishes a bad release from a bug.

## Test surface

```bash
pytest -q          # 157 passed
```

| Module | LOC | Tests |
|---|---|---|
| `signals.py` | 382 | 23 |
| `plan.py` | 184 | 25 |
| `safety.py` | 278 | 28 |
| `gitops.py` | 430 | 33 |
| `manifest.py` | 204 | (covered via gitops) |
| `verify.py` | 171 | 9 |
| `journal.py` | 435 | 16 |
| `serve.py` | 397 | 15 |
| `cli.py` | 536 | 8 |
| `model.py` | 224 | — |
| **Total** | **3,248** | **157** |

Three testing decisions are load-bearing, and each was chosen because the cheaper alternative would have passed against broken code:

- **GitOps tests run against a real temporary git repository**, not mocks. The behaviour under test is what git actually does with history, branches and a dirty tree.
- **Verification tests inject a fake clock**, which is what lets a 180-second timeout be exercised in milliseconds — and which surfaced a genuine infinite loop when a no-op sleep left the clock stalled.
- **Console tests drive a real socket.** An in-process call to the router would have passed happily against a design that returned empty results for every request after the first; see [`FINDINGS.md`](FINDINGS.md).

Zero runtime dependencies is verifiable rather than asserted: a clean-environment install reports only the package itself.

```bash
python3.12 -m venv /tmp/check && /tmp/check/bin/pip install -q .
/tmp/check/bin/pip list --format=freeze     # kubemend==0.1.0
```

CI runs the suite on Python 3.10 and 3.13, plus a separate job that installs k3d, creates a real cluster, and runs the full demo end to end.

## What has not been evaluated

Stated plainly, because the gaps are more informative than the numbers:

- **No production deployment.** Every result here is from a fixture or a throwaway cluster.
- **No accuracy measurement on real incidents.** There is no labelled corpus of "what a human would have done", so precision and recall of the *planner* are unmeasured. The revert rate is a proxy for it, and only for the cases where the agent acted.
- **No false-negative measurement.** Nothing measures the problems detection misses.
- **No load or scale testing.** Behaviour on a cluster with thousands of workloads is unknown.
- **No multi-workload verification.** Recovery is confirmed for the treated workload only.
- **Verification is only measured on `image_pull`.** Both live scenarios are pull failures; the other seven rules have unit coverage but no live end-to-end verification run.

---

Further reading: [`THREAT-MODEL.md`](THREAT-MODEL.md) for what the design prevents, [`FINDINGS.md`](FINDINGS.md) for what building it turned up, and [`kubemend-report.pdf`](kubemend-report.pdf) for the full write-up.
