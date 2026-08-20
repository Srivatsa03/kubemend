---
title: "Building a Kubernetes agent that cannot do anything catastrophic"
published: false
description: "How to design a remediation agent so its safety properties are structural instead of behavioral: a closed action set, a pure-function policy gate, surgical manifest edits, and verification that reverts its own work."
tags: kubernetes, devops, sre, testing
cover_image:
---

Every AI SRE tool will read your cluster and tell you what is wrong. Almost none
of them are trusted to *act*, and the reason is not model quality. Nobody has a
convincing answer to "what stops it doing something catastrophic at 3am," and
"the model is usually careful" is not an answer. It is a statement about average
behavior offered in response to a question about worst case behavior.

I spent a few weeks building one where every safety property is code with tests
rather than an instruction in a prompt. This is the whole design, start to
finish, including the parts I got wrong.

The tool is [kubemend](https://github.com/Srivatsa03/kubemend). Python 3.10+,
zero runtime dependencies, 164 tests.

## The constraint everything else follows from

An agent that emits shell commands or freely generated manifests has an unbounded
action space. Three properties then become unavailable *in principle*, and no
amount of model quality restores them:

1. You cannot enumerate what it might do, so you cannot review it in advance.
2. You cannot compute the blast radius before it runs.
3. There is no mechanical inverse, so recovery is a judgement call under pressure.

So the first decision is where the write lands. kubemend holds no cluster
credentials and issues no write to the Kubernetes API. **Its only write surface
is a commit to the GitOps repository that already defines your cluster.** Argo CD
or Flux carries it the rest of the way.

That one choice inherits an entire safety apparatus instead of reimplementing it:

- An audit trail, because git is one.
- Review before rollout, because a pull request is the native unit of review.
- Rollback, because `git revert` has been correct for twenty years.

It also imposes a real constraint, which is the point. An action that cannot be
expressed as an edit to a manifest cannot be performed at all.

## Step 1: detection produces typed findings

Eight rules run over cluster JSON collected with read-only verbs. Detection is
the least constrained part of the system on purpose, because being wrong here
costs a false report rather than a bad change.

| Rule | Severity | Catches |
|---|---|---|
| `crashloop` | critical | Container the kubelet gave up restarting promptly |
| `oomkilled` | critical | Killed by the kernel for exceeding its memory limit |
| `image_pull` | critical | Bad tag, bad registry, missing pull credentials |
| `config_error` | critical | References a ConfigMap or Secret that does not exist |
| `rollout_stuck` | critical | Rollout past its progress deadline |
| `replica_shortfall` | critical / warning | Fewer replicas available than desired |
| `unschedulable` | warning | No node can fit the pod |
| `flapping` | warning | Restarting repeatedly while still reporting healthy |

`flapping` is the one worth calling out. The pod is up, dashboards are green,
nothing pages, and the container has died eleven times today. That is the failure
mode most likely to survive unnoticed for weeks, and it is invisible to any check
that asks only whether pods are running.

## Step 2: a closed set of six typed actions

kubemend never emits commands. It selects from six typed actions, and every
action carries both the state it found and the state it intends.

```python
class ActionKind(str, Enum):
    SCALE = "scale"                  # change replica count
    ROLLBACK = "rollback"            # revert to a prior revision
    RESTART = "restart"              # trigger a rolling restart
    SET_RESOURCES = "set_resources"  # adjust requests/limits
    SET_IMAGE = "set_image"          # pin or correct an image
    SET_PROBE = "set_probe"          # adjust probe timings
```

Three properties follow from the *type*, not from the agent's care:

- **Reversible by construction.** An action holds `before` and `after`, so its inverse is a field swap. An action whose `before` could not be captured is refused up front rather than discovered to be irreversible at rollback time.
- **Blast radius computable before execution.** Every action declares the pods it disrupts, so a plan is measured and refused while it is still text.
- **Reviewable.** A typed action renders to a deterministic diff.

Adding a kind to that enum is a security decision, not a feature decision.

## Step 3: abstention is a designed output

Findings are grouped per workload, and each incident yields at most one plan. On
the fixture shipped in the repo, **13 findings produce 4 plans and 5 findings
produce no action at all.**

Those are not gaps. A missing ConfigMap needs a value the agent has no business
inventing. An unschedulable pod is a capacity decision. A container restarting
for unclear reasons needs a human to read the logs.

An agent that acted on all thirteen would be worse, not more capable. The test
suite **asserts** the abstentions rather than tolerating them, so making the
planner more aggressive later requires a deliberate argument.

## Step 4: the gate, a pure function with no model in it

```python
verdict = gate(plan, policy)
```

Six properties, checked before anything is written:

1. **Protected namespaces refused outright.** Twelve of them (`kube-system`, `argocd`, `flux-system`, ...). Not a threshold, a hard refusal. Touching them can remove the machinery you would use to recover.
2. **Only action kinds named in policy.** Deny by default.
3. **Blast radius bounded** on pods, workloads, and namespaces, for the plan as a whole, since three individually harmless restarts are not a harmless change.
4. **No action without a computable undo.**
5. **Rate limiting.** A cluster that has needed several fixes this window is a human's problem, not a loop's.
6. **An unconfigured `Policy()` permits nothing.**

Trust is granted per action class through three autonomy levels: `report`
(findings only), `propose` (a branch and a PR), `apply` (committed unattended).
Under the shipped conservative policy only `rollback` may apply unattended,
because it moves a workload to a state that demonstrably ran in this cluster.

**A plan's autonomy is the minimum across its actions.** One action needing review
holds back the whole plan, because applying the safe half of a plan that was
reasoned about as a unit is frequently worse than applying none of it.

Here is the gate working on the fixture:

| Workload | Action | Conservative | Staging |
|---|---|---|---|
| `payments/checkout` | `rollback` | apply | apply |
| `jobs/report-worker` | `rollback` | apply | apply |
| `payments/api` | `set_resources` | **propose** | apply |
| `kube-system/coredns` | `rollback` | **refused** | **refused** |

The identical `rollback` is applied on one workload and refused on another,
purely on namespace. And loosening policy changes the *destination* of a plan,
not permission: `kube-system` stays refused under both.

## Step 5: edit the manifest surgically, not with a YAML library

This is the step I expected to be boring and was not.

The obvious implementation parses the YAML, mutates the object, and serializes it
back. That produces a **correct file** and a **useless commit**. The serializer
rewrites the whole document: comments dropped, keys reordered, quoting
normalized. A reviewer expecting to check one number is shown three hundred
changed lines.

Then the actual failure happens. Nobody reads three hundred lines of reformatted
YAML at 3am. They skim it, see it is machine generated, and approve. **The review
step still exists in the diagram and has stopped being a control.**

If review is your safety mechanism, diff size is a safety property. So kubemend
does surgical text edits against an indentation-path tracker:

```diff
-          image: nginx:1.27-alpine-typo   # known good
+          image: nginx:1.27-alpine   # known good
```

One line. The trailing comment survives with its exact spacing, because the
original gap is measured and reproduced rather than normalized:

```python
gap = len(body) - len(body.rstrip())
comment = " " * gap + "#" + after
```

Fussy, and worth it. A diff where the comment column shifts is a diff a reviewer
reads twice, and every extra token of reading is a chance the review stops being
real.

**The cost, stated honestly:** hand written text manipulation of YAML is more
fragile than a parser and needed its own test suite. One bug had a second
container nesting inside the first, because sibling list elements close each
other at *equal* indentation while plain keys do not:

```python
limit = (lambda d: d >= indent) if is_item else (lambda d: d > indent)
```

### Rollback is a git operation, not a computed edit

Worth its own heading, because it only became obvious while implementing it. In a
GitOps repository the manifest *is* the source of truth, so returning a workload
to its previous revision is not a computed edit at all:

```python
_git(self.path, "show", f"{previous}:{relative}")
```

Nothing is reconstructed. The prior version is in the history byte for byte,
comments and key order included. A rollback implemented against the Kubernetes
API would have to *rebuild* the previous state and would get the incidentals
wrong. Here the incidentals are not incidental. They are the file.

## Step 6: verify, and withdraw your own work

A loop that stops at "committed" is half a loop. The agent acted on a diagnosis
that may have been wrong, and until something checks, the cluster is in a state
nobody has confirmed is better than the one it replaced.

Recovery is defined narrowly: the findings that motivated the change are gone,
**and** no new critical finding appeared on that workload. Not "the pods are
running," which is true moments before a crash loop starts.

Three outcomes, and only one of them undoes anything:

| Outcome | Meaning | Reverts? |
|---|---|---|
| `recovered` | motivating findings gone | no |
| `still_failing` | they remain, or a new critical one appeared | **yes** |
| `indeterminate` | the cluster could not be read | no |

Two decisions here matter more than the polling.

**Silence is not success, and it must break the streak.** Recovery requires two
consecutive clean reads, because a rollout looks briefly healthy as it begins. A
failed poll does not merely fail to count, it *resets* the streak. The sequence
`clean -> error -> clean` must not read as recovered, because two clean reads
either side of a blind one are not two consecutive observations.

**Indeterminate does not trigger a revert.** Reverting on evidence of continued
failure is right. Reverting because the API server was briefly unreachable would
undo a fix that may have worked, on no evidence, and turn a network blip into a
second unplanned production change.

## Step 7: keep score

Every run writes what it saw, decided, wrote, and confirmed to an append only
SQLite file. That produces a number I have not seen other tooling in this space
report:

```
    incidents   2
    committed   2
    verified    1
    revert rate 50%  (1 of 2 fixes did not hold)

  keeps coming back   a rollback is not going to fix these
      2x  payments/checkout
```

**Revert rate is the agent grading itself on real outcomes.** It is the only
honest input to "should this be allowed to `apply` rather than `propose`?" It
only exists because verification exists: without a check after the change, "did
the fix work" has no recorded answer and the loop can report success for anything
it managed to commit.

**Repeat offenders** are the same idea over time. A workload remediated weekly
does not have a bad release, it has a bug, and without a history three successful
rollbacks look like three successes.

That 50 percent is a property of a demo built so one of its two scenarios cannot
be fixed by a rollback. It shows the measurement working. It is not a production
accuracy claim.

## The bug that only appeared with a real reconciler

My demo used `kubectl apply` where Argo CD would be. When I finally installed the
real thing, it found that **`apply` was committing and never pushing.**

With `kubectl apply` reading my working tree, a local commit *was* the delivery,
so the code was correct for the stand in and wrong for everything it stood in
for. Against a reconciler watching a remote the commit does not exist, the
workload never recovers, verification correctly reports `still_failing`, and the
agent reverts a fix that was *correct*.

Thirty three tests covered that path. None had a remote.

I wrote that up separately, because the lesson generalizes past Kubernetes: a
stand in does not just leave a gap in your coverage, it satisfies a requirement
the real dependency would have enforced and hides every defect that depends on
it.

## What is not true

The planner is **deterministic**. There is no model in the decision path. That is
sequencing rather than limitation: the parts that must be trustworthy are code
with tests, so when a model is added it correlates and explains rather than
deciding what happens to your cluster. Do not call this an LLM agent, because it
is not one.

Version 0.1.0, alpha. Deployments only. Verification is single workload, so it
confirms the treated workload recovered, not that the change was harmless to its
dependents.

## Try it

The analysis path runs with no cluster and nothing installed:

```bash
git clone https://github.com/Srivatsa03/kubemend
cd kubemend && pip install -e ".[dev]"

kubemend diagnose --snapshot fixtures/broken-cluster.json
kubemend policy          # what each policy permits
```

The full loop against a throwaway cluster, both outcomes:

```bash
demo/run.sh              # needs k3d, kubectl, Docker
kubemend log             # what it knows about itself
kubemend serve           # read-only console on localhost
```

CI runs that same script against a real cluster on every push, because a project
whose claim is "it works against a real cluster" should not prove it with mocks.
