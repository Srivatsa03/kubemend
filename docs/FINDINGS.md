# An agent is trustworthy in proportion to what it cannot do

Seven findings from building a remediation agent that is allowed to change production.

**A note on what kind of document this is.** These are *engineering* findings, not empirical results. They came out of building the system and watching it fail in specific ways, and the evidence for each is a design consequence or a real bug, not a controlled experiment. Where a number appears it is reproducible ([`EVALUATION.md`](EVALUATION.md)); where a judgement appears it is labelled as one. The distinction matters because the temptation in this space is to dress design opinions as measurements, and that is precisely the habit that makes agent tooling hard to evaluate.

## Summary

| # | Finding |
|---|---|
| 1 | Reversibility is an architectural property. Chosen well, it is inherited rather than built. |
| 2 | A correct change can be an unreviewable change, and unreviewable changes make review theatre. |
| 3 | "Silence is not success" is not enough — silence must also break the streak. |
| 4 | Failing safe and failing silently are the same mechanism seen from two sides. |
| 5 | Abstention is the majority behaviour, so it has to be a designed output rather than a fallthrough. |
| 6 | The measurement that decides trust is the one nobody publishes. |
| 7 | A stand-in for a dependency hides the bugs that only the real one causes. |

The thread running through all seven: **a safety property that holds because the code cannot express the alternative survives contact with production. A property that holds because the system is usually careful does not.** Most of the work below is moving properties from the second category into the first.

---

## Finding 1: reversibility is architectural, not a feature

**Claim.** Undo is either a consequence of where you put the write, or it is a subsystem you will build badly.

The naive design gives the agent cluster credentials and an "undo" implementation: record what you changed, and on failure, change it back. That subsystem has to handle partial application, concurrent modification, and its own failure — and it is exercised for the first time during an incident, which is the worst possible moment to discover it is wrong.

Routing the write through git deletes the subsystem. Undoing the agent is `git revert`, which is not a feature anyone had to build, has been correct for twenty years, and every operator on the team already knows at 3am.

**The sharper version of this only became visible while implementing rollback.** In a GitOps repository the manifest *is* the source of truth, so returning a workload to its previous revision is not a computed edit at all. It is restoring the file to its previous committed state:

```python
_git(self.path, "show", f"{previous}:{relative}")
```

Nothing is reconstructed. The prior version is in the history byte for byte — comments, key order, quoting, all of it. A "rollback" implemented against the Kubernetes API would have to *rebuild* the previous state and would get the incidentals wrong; here the incidentals are not incidental, they are the file.

**What it implies.** When evaluating an agent that writes, ask where the write lands before asking how good the model is. The write target determines which safety properties are available at all.

---

## Finding 2: a correct change can be an unreviewable change

**Claim.** If review is your safety mechanism, then diff size is a safety property — and the obvious implementation destroys it.

The obvious way to edit a manifest is to parse the YAML, mutate the object, and serialise it back. This produces a **correct file** and a **useless commit**. The serialiser rewrites the whole document: comments dropped, keys reordered, quoting normalised, anchors expanded. A reviewer opens a pull request expecting to check one number and is shown three hundred changed lines.

What happens next is the actual failure. Nobody reads three hundred lines of reformatted YAML at 3am. They skim it, see it is machine-generated, and approve. **The review step still exists in the process diagram and has stopped functioning as a control.** The safety story — "a human reviews every change before it reaches the cluster" — is now false while remaining literally true.

So kubemend rejects the YAML library and performs surgical text edits against an indentation-path tracker. The output:

```diff
-          image: nginx:1.27-alpine-typo   # known good
+          image: nginx:1.27-alpine   # known good
```

One line. The trailing comment survives, including the exact whitespace before it — the original gap is measured and reproduced rather than normalised:

```python
gap = len(body) - len(body.rstrip())
comment = " " * gap + "#" + after
```

That is a fussy detail with a real justification: a diff where the comment column shifts is a diff a reviewer has to read twice, and every extra token of reading is a chance the review stops being real.

**Cost, stated honestly.** Hand-written text manipulation of YAML is more fragile than a parser and needed its own test suite. One bug — a second container nesting inside the first — came from popping the path stack at the wrong depth, fixed by recognising that sibling list elements close each other at *equal* indentation while plain keys do not:

```python
limit = (lambda d: d >= indent) if is_item else (lambda d: d > indent)
```

That fragility is the price of reviewable diffs, and it is worth paying, because a parser buys correctness of the file at the cost of correctness of the process.

---

## Finding 3: silence must break the streak, not merely fail to extend it

**Claim.** "A failed read does not count as success" is the obvious rule and it is insufficient. A failed read must actively reset the evidence.

Verification requires **two consecutive clean reads** before declaring recovery, because a rollout looks briefly healthy as it begins. The obvious handling of a failed poll is: it proves nothing, so skip it and carry on.

That is wrong, and the sequence that shows it is `clean → error → clean`. Under "skip it", the streak reaches two and the workload is declared **recovered** — on the strength of two observations taken either side of a blind spot, with an unobserved interval between them during which anything could have happened. The rule says *consecutive*; skipping quietly redefines the word.

```python
result.errors += 1
snapshot = None
# An unreadable poll breaks the recovery streak. Two clean reads
# either side of a blind one are not two consecutive observations,
# and letting silence bridge them would contradict the rule this
# module is built on.
clean_streak = 0
```

**How this was actually caught is the useful part.** A test asserted the correct behaviour and failed. The tempting move was to fix the test — it was newer than the implementation, and the implementation had been reviewed. Instead the module's own docstring settled it: it states "silence is not success", and skipping a failed read lets silence contribute to a success verdict. **The implementation was wrong and the stated contract was the authority.**

**The corollary points the other way, and is equally important.** An unverifiable outcome must *not* trigger a revert. Reverting on positive evidence of continued failure is right; reverting because the API server was briefly unreachable would undo a fix that may have worked, on no evidence, and convert a network blip into a second unplanned production change. So `indeterminate` stops and asks for a human.

Silence is not success, but it is not failure either. Both halves are required, and they pull in opposite directions.

---

## Finding 4: failing safe and failing silently are the same mechanism

**Claim.** Every "this must never break the main path" component is also a component that can hide its own defects, and the second property arrives free with the first.

The incident log is deliberately non-load-bearing. Every write is wrapped, an unwritable journal degrades to a note in the output, and the run continues:

```python
except sqlite3.Error as exc:
    self.available = False
    self.error = str(exc)
```

The reasoning is sound. Failing to fix a broken cluster because a log file was read-only would be a poor trade.

**Then the console was built on the same journal, and returned nothing.** SQLite connections are bound to their creating thread; the server was threaded; every request after the first raised — and the raise was swallowed by the very error handling that exists so bookkeeping cannot break remediation. The user-visible symptom was an empty list, rendered as "no incidents".

That is the worst available failure mode: **a wrong answer that is indistinguishable from a valid one.** A crash would have been better. An error banner would have been better. "No incidents recorded" is a sentence the reader has no reason to doubt.

Two consequences were adopted:

- The console opens a connection **per request** rather than sharing one. This is cheap for SQLite and has the welcome side effect that a console reading a file a `remediate` run is still appending to shows what was just written.
- The regression test drives a **real socket**. An in-process call to the router would have passed happily against the broken design, so the test was verified to fail against the old code before being trusted:

  ```
  rows via the creating thread: 1
  rows via a server thread:     0
  ```

**What it implies.** When a component is designed to swallow errors, something outside it must be able to see them. The journal exposes `available` and `error`, and the CLI prints them — that path existed, but nothing consumed it on the read side.

---

## Finding 5: abstention is the majority behaviour

**Claim.** For a bounded agent, "no safe action exists" is the most common correct output, so it has to be a designed result rather than what happens when nothing matches.

On the shipped fixture, **13 findings produce 4 plans, and 5 findings produce no action at all.** Of the 4 plans, one is refused outright. Counting end to end, the majority of what the agent sees, it does not act on.

The reasons are not deficiencies:

- A missing ConfigMap needs a **value** the agent has no business inventing.
- An unschedulable pod is a **capacity decision**, not a manifest error.
- A container restarting for unclear reasons needs a **human to read the logs**.

An agent that acted on all thirteen would be worse, not more capable. So abstention is a first-class output — the finding is reported with its evidence and no plan is produced — and the test suite **asserts** the abstentions rather than merely tolerating them. That distinction matters: a test that asserts a plan is not produced will fail if someone later makes the planner more aggressive, which is exactly the change that should require a deliberate argument.

The same principle shapes the gate. An unconfigured `Policy()` permits nothing. A permissive default is the difference between a bounded assistant and an unsupervised process with cluster credentials.

**Related, and easy to get wrong:** a plan's autonomy is the **minimum** across its actions. One action requiring review holds back the whole plan, because applying the safe half of a plan that was reasoned about as a unit is frequently worse than applying none of it.

---

## Finding 6: the measurement that decides trust is the one nobody publishes

**Claim.** The question gating autonomy is "how often is it wrong when it acts?", and almost no tool in this space reports it — because reporting it means saying out loud that the agent is sometimes wrong.

Detection rates are published constantly. They are also not the blocker: nobody is stuck on whether a `CrashLoopBackOff` can be identified. The blocker is whether the thing may act unattended, and that question needs a different number.

kubemend records every run — findings, plan, verdict, commit, verification outcome, revert — and reports:

```
    revert rate 50%  (1 of 2 fixes did not hold)

  keeps coming back   a rollback is not going to fix these
      2x  payments/checkout
```

**Revert rate is the agent grading itself on real outcomes.** It only becomes computable because verification exists: without a check after the change, "did the fix work?" has no recorded answer, and the loop can report success for anything it managed to commit.

**Repeat offenders** are the same idea over time. A workload remediated weekly does not have a bad release, it has a bug — and without a history, three successful rollbacks look like three successes rather than one unaddressed defect. Refusals are logged too: what the gate declined to touch, and why, is as much a part of the audit trail as what it changed.

**The honest caveat, which belongs next to the number every time it is quoted.** The 50% above comes from a demo deliberately built so one of its two scenarios is unfixable by rollback. It demonstrates that the measurement works end to end. It is not kubemend's production accuracy, that figure has not been measured, and quoting it as such would be quoting a demo parameter.

**What it implies.** A revert rate is what should earn an agent the promotion from `propose` to `apply` — measured on that workload, in that cluster, by that agent. A tool that declines to measure it is asking for trust it has not earned.

---

## Finding 7: a stand-in hides the bugs only the real thing causes

**Claim.** Substituting a simpler thing for a dependency does not just leave a
gap in coverage. It actively conceals defects, because the substitute quietly
satisfies a requirement the real dependency would have enforced.

`demo/run.sh` used `kubectl apply` where Argo CD or Flux would be. That seemed
like an honest simplification — the demo said so in a comment, and the agent's
contract ends at the commit either way.

It was not. Standing up real Argo CD against a real GitHub repository exposed
this immediately:

> **`apply` committed locally and never pushed.**

With `kubectl apply` reading the working tree, a local commit *was* the
delivery, so the code was correct for the substitute and wrong for everything
it stood in for. Against a reconciler watching a remote, the commit is
invisible. And the failure compounds in the worst available direction: the
workload never recovers, verification reports `still_failing`, and the agent
**reverts a fix that was correct but undelivered** — concluding its diagnosis
was wrong when the only thing wrong was delivery.

Thirty-three tests covered `gitops.py`. None caught it, because none had a
remote. The substitute had defined the interface they were written against.

Two further defects surfaced in the same session, both from finally exercising
a remote:

- **Every reported revert SHA was dangling.** `revert()` read `HEAD` before
  `commit --amend`, and amending replaces the commit object. The value the
  journal stored and the CLI printed as `reverted in ...` pointed at a commit
  not in the history.
- **The suite went from 13s to 346s** the moment pushes appeared, because git
  was sitting at credential prompts until each 30-second timeout. That is a
  test-suite annoyance and a production defect wearing the same clothes: an
  unattended agent must never be the process waiting at a password prompt.
  `_git` now runs with no terminal prompt, no askpass, non-interactive SSH, and
  no inherited stdin.

**What it implies.** When a stand-in sits where a dependency will, ask what the
real one enforces that the substitute does not. Here it was one word —
*delivery* — and the substitute answered it for free. The generalisation is
uncomfortable: the more convenient the stand-in, the more requirements it is
silently satisfying on your behalf.

## What this means if you are building one

1. **Decide where the write lands before anything else.** It determines which safety properties are available at all, and no amount of model quality recovers a property the architecture forecloses.
2. **Treat diff size as a safety property** if your story involves human review. A change nobody can read is a change nobody reviews.
3. **Define recovery narrowly and require it to hold.** "The pods are running" is true moments before a crash loop starts.
4. **Separate "no evidence" from "evidence of failure"** and give them different consequences. Collapsing them produces either false confidence or gratuitous churn.
5. **Assume every fail-safe component can hide its own bugs,** and give something outside it visibility.
6. **Publish the accuracy number**, including when it is unflattering. It is the one that decides whether the thing gets to act.
7. **Run it against the real dependency early.** A stand-in does not merely fail to test things; it satisfies requirements on your behalf and hides the defects that violate them.

## Limits of these findings

- **Single system, single author.** These are observations from building one agent, not a survey of the design space, and another team could reach different conclusions with a different write target.
- **Not empirically validated.** Finding 2 asserts that large diffs cause rubber-stamped reviews. That is a widely-held belief and a plausible mechanism; it is not something measured here.
- **No production deployment.** Every finding is grounded in a fixture, a throwaway cluster, or a real bug during development. None is grounded in operating this at scale.
- **The planner is deterministic.** Findings about the acting half hold. Nothing here is evidence about how a model-driven planner would behave, and Finding 1's reasoning is exactly why the model is scheduled to correlate and explain rather than to decide.

## Reproduce

```bash
# Finding 5: abstention on the recorded fixture
kubemend diagnose --snapshot fixtures/broken-cluster.json

# Findings 1, 2, 3, 6: both terminal outcomes on a real cluster
demo/run.sh

# Finding 6: the numbers the log produces
kubemend log
kubemend serve

# Findings 3 and 4: the tests that caught them
pytest tests/test_verify.py tests/test_serve.py -q
```

---

Further reading: [`THREAT-MODEL.md`](THREAT-MODEL.md) for what the design prevents, [`EVALUATION.md`](EVALUATION.md) for the measurements, and [`kubemend-report.pdf`](kubemend-report.pdf) for the full write-up.
