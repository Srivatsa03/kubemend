# kubemend

**A Kubernetes SRE agent whose only write surface is a git commit.**

Every AI SRE tool will read your cluster and tell you what it thinks is wrong. Almost none of them are trusted to *act*, and the reason is not model quality. It is that nobody has a convincing answer to "what stops it doing something catastrophic at 3am," and "the model is usually careful" is not an answer.

kubemend is an attempt at the answer. It diagnoses freely and acts narrowly, and every constraint on it is code with tests rather than a prompt.

```
  remediation   one plan per incident, gated independently

    ✓ APPLY    payments/deployment/checkout
        rollback payments/deployment/checkout (revision: 12 -> 11)
        ↳ rollout_stuck: rollout of checkout exceeded its progress deadline
        undo: rollback payments/deployment/checkout (revision: 11 -> 12)

    ✓ PROPOSE  payments/deployment/api
        set_resources payments/deployment/api (memory: '256Mi' -> '512Mi')
        ↳ container was OOMKilled at 256Mi
        · [autonomy_ceiling] held at 'propose' by action(s): set_resources

    ✗ REFUSED  kube-system/deployment/coredns
        rollback kube-system/deployment/coredns (revision: 5 -> 4)
        · [protected_namespace] kube-system is protected

  no automated fix   reported for a human

    · jobs/deployment/emailer            config_error, replica_shortfall
    · analytics/deployment/ingest        replica_shortfall, unschedulable
    · web/deployment/frontend            flapping
```

## Try it

No cluster needed. The repo ships a recorded snapshot of a cluster having a bad afternoon.

```bash
pip install -e ".[dev]"
kubemend diagnose --snapshot fixtures/broken-cluster.json
kubemend policy          # what each shipped policy permits
```

Against a real cluster, using your existing kubeconfig and read-only verbs:

```bash
kubemend diagnose                       # all namespaces
kubemend diagnose -n payments           # one namespace
kubemend snapshot --out cluster.json    # record now, analyse later
```

## Writing the fix

The agent has no cluster credentials and makes no API calls. It edits a manifest in your GitOps repository and commits; Argo CD or Flux carries the change to the cluster. Everything the repository already gives you then applies to the agent for free — an audit trail, review before rollout, and a revert that is one command.

```bash
kubemend remediate --snapshot cluster.json --repo ~/gitops --dry-run
kubemend remediate --repo ~/gitops              # against a live cluster
```

```
    ✓ APPLY    payments/deployment/checkout
        restored clusters/prod/checkout.yaml to fee63e44
        commit fc5d427c on main
        -          image: reg.internal/checkout:2.4.0   # last known good
        +          image: reg.internal/checkout:2.3.0   # last known good

    ✓ PROPOSE  payments/deployment/api
        spec.template.spec.containers.api.resources.limits.memory: 256Mi -> 512Mi
        commit 0efe9c6 on kubemend/payments-api

    ✗ REFUSED  kube-system/deployment/coredns
        · kube-system is protected
```

Policy decides the destination, not just permission: a rollback trusted to `apply` commits to the mainline, a resource change held at `propose` becomes a branch to review, and a refused plan is never rendered at all.

### Rollback is a git operation

In a GitOps repository the manifest is the source of truth, so returning a workload to its previous revision means restoring the file to its previous committed state. Nothing is reconstructed — the prior version is in the history, byte for byte, comments and all. This falls out of the architecture rather than being engineered, and it is the strongest reason to route an agent's actions through git.

### The diff is one line

The obvious way to edit a manifest is to parse the YAML, mutate the object, and dump it back. That produces a correct file and a useless commit: the dumper rewrites the whole document, drops comments, reorders keys, and normalises quoting, so a reviewer sees three hundred changed lines when the change was one number.

The entire value of routing actions through git is that a human can read the diff before it reaches the cluster, so edits are surgical. Trailing comments survive, including the exact spacing before them.

### It refuses more than it writes

- **A workload with no manifest** is reported, never created.
- **A value that drifted** — the repo says something other than what the cluster reported — is left alone. Somebody changed it, and their change is not the agent's to discard.
- **A field that does not exist** is not added. Inventing a resource limit the author never wrote is a different kind of change from adjusting one that is there.
- **An action with no manifest representation**, such as a rolling restart, is refused with a reason rather than approximated.
- **A dirty working tree** blocks emission entirely, so an agent's commit never absorbs someone's in-progress edit.

Every commit carries the evidence that produced it — the findings, the reason, the blast radius, what was deliberately left alone — and ends with the line that matters: `Undo: git revert this commit.`

## The design

### Typed actions, not commands

An agent that emits shell commands or raw manifests has an unbounded action space. You cannot enumerate what it might do, you cannot compute the blast radius before it runs, and you cannot mechanically undo it. Every safety property you would want is unavailable in principle, not merely unimplemented.

So kubemend never emits commands. It selects from a **closed set of six typed actions**, and every action carries both the state it found and the state it intends. Three properties follow directly:

- **Reversible by construction.** An action holds `before` and `after`, so its inverse is a field swap. An action whose `before` could not be captured is refused up front, rather than discovered to be irreversible at rollback time.
- **Blast radius is computable before execution.** Every action declares the pods it disrupts, so a plan is measured and refused while it is still text.
- **Reviewable.** A typed action renders to a deterministic diff. A human reviewing a pull request sees a bounded change, not a prompt's output.

Adding an action kind is a security decision, not a feature decision.

### Three levels of autonomy, granted per action

Trust is earned per action class, mirroring the crawl/walk/run progression operations teams actually use:

| Level | What happens |
|---|---|
| `report` | Findings only. Nothing is proposed. |
| `propose` | A pull request, for human review. |
| `apply` | Committed to the GitOps branch unattended. |

Under the shipped conservative policy only **rollback** may apply unattended, because it moves a workload to a state that demonstrably ran in this cluster and its inverse is exact. Everything else waits for a human.

**A plan's autonomy is the minimum across its actions.** One action needing review holds back the whole plan, because applying the safe half of a plan that was reasoned about as a unit is frequently worse than applying none of it.

### What the gate enforces

Six properties, checked by a pure function with no model in the loop:

- Control-plane namespaces (`kube-system`, `argocd`, `flux-system`, …) are excluded outright, not guarded by a threshold. Touching them can remove the machinery that would let you recover.
- Only action kinds named in policy, everything else denied by default.
- Blast radius bounded on pods, workloads and namespaces — for the plan as a whole, since three individually harmless restarts are not a harmless change.
- No action without a computable undo.
- Flap protection: a cluster that has already needed several fixes this window is a human's problem, not a loop's.
- An unconfigured `Policy()` permits nothing. A permissive default here is the difference between a bounded assistant and an unsupervised process with cluster credentials.

Policy is data, not code, so the rules governing an autonomous system are themselves reviewable, diffable and revertible — the same property the agent's own actions are required to have.

### One plan per incident

An incident is something that happened to a service, and a responder handles them one at a time. Batching every unrelated problem into one change would trip every blast-radius limit at once and couple the fate of unrelated services. Separate plans are separately reviewable, appliable and revertible.

### Abstaining is a feature

In the demo above, **10 of 13 findings produce no action at all.** A missing ConfigMap needs a value the agent has no business inventing. An unschedulable pod is a capacity decision. A container restarting for unclear reasons needs a human to read the logs. Reporting these clearly and stopping is the correct behaviour, and the tests assert it.

## Status

Early. Detection, planning, the policy gate and GitOps emission are implemented and tested (105 tests, no dependencies). The planner is **deterministic today** — there is no model in it yet, and that is sequencing rather than limitation: the parts that must be trustworthy are code with tests, so when a model is added it correlates and explains rather than deciding what happens to your cluster.

Not yet built:
- Opening a pull request from the branch (the branch and commit exist; `gh pr create` does not run yet)
- Verifying the fix worked, and auto-reverting when it did not
- A model layer for correlating findings and writing incident narrative
- StatefulSets, DaemonSets, Jobs; node-level and networking signals

## Detection rules

| Rule | Severity | What it catches |
|---|---|---|
| `crashloop` | critical | Container the kubelet has given up restarting promptly |
| `oomkilled` | critical | Killed by the kernel for exceeding its memory limit |
| `image_pull` | critical | Bad tag, bad registry, or missing pull credentials |
| `config_error` | critical | References a ConfigMap or Secret that does not exist |
| `rollout_stuck` | critical | Rollout past its progress deadline |
| `replica_shortfall` | critical / warning | Fewer replicas available than desired |
| `unschedulable` | warning | No node can fit the pod |
| `flapping` | warning | Restarting repeatedly while still reporting healthy |

`flapping` is the one worth calling out: the pod is up, dashboards are green, nothing pages, and it has died eleven times today. That is the failure mode most likely to survive unnoticed for weeks.

## License

Apache-2.0
