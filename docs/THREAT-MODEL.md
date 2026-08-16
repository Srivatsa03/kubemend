# Threat model

What can go wrong when an automated process is allowed to change production, which of those things this design structurally prevents, and which it does not.

The distinction that organises this document is between **prevented** and **discouraged**. A property that holds because the code cannot express the alternative is prevented. A property that holds because the agent is usually sensible is discouraged, and discouraged properties are worth exactly nothing at 3am. kubemend tries to move as much as possible into the first category and to be honest about what remains in the second.

## The core problem

The dangerous thing about an autonomous remediation agent is not that it might misdiagnose. Humans misdiagnose constantly and production survives, because the blast radius of a human's mistake is bounded by how fast they can type and how quickly a colleague can stop them.

The dangerous thing is **an unbounded action space combined with an unattended trigger**. An agent that emits shell commands or free-form manifests can, in principle, delete a namespace, scale a database to zero, or edit the very controller that would let you recover — and it can do so at 3am, repeatedly, faster than anyone can intervene.

Three properties become unavailable *in principle* under an unbounded action space, and no amount of model quality restores them:

1. You cannot enumerate what it might do, so you cannot review it in advance.
2. You cannot compute the blast radius of a proposal before it executes.
3. There is no mechanical inverse, so recovery is itself an act of judgement under pressure.

Everything below follows from trying to get those three back.

## Adversary model

kubemend is not primarily defending against a human attacker. Its adversary is **the agent itself under bad inputs** — a misread of cluster state, a wrong plan, a correct plan applied to the wrong workload, a fix that makes things worse. That framing matters: an untrusted-input attacker gets one section, and the rest of the document treats the agent as the hazard.

| Actor | Capability assumed | In scope |
|---|---|---|
| The agent, misdiagnosing | Full read of cluster state; may reach any conclusion | **Yes** — the primary concern |
| The agent, looping | May re-trigger repeatedly, unattended | **Yes** |
| A hostile cluster state | Pod names, images, messages, annotations are attacker-influenced strings | **Partly** — see below |
| A compromised GitOps repo | Can write anything the reconciler will apply | **No** — out of scope |
| A compromised kubeconfig | Can do anything to the cluster directly | **No** — out of scope |

The last two are out of scope because kubemend is strictly less capable than either. If an attacker holds your GitOps repository or your cluster credentials, they do not need the agent, and hardening the agent against them accomplishes nothing.

## The catalog

Each entry states the failure, then how the design responds and how strong that response is.

### 1. Catastrophic action (`unbounded action space`)

**Failure.** The agent takes an action nobody anticipated — deletes a resource, drains a node, edits an admission webhook.

**Response — prevented.** kubemend never emits commands. It selects from a closed set of six typed actions (`scale`, `rollback`, `restart`, `set_resources`, `set_image`, `set_probe`). There is no code path that produces anything else. Deleting a resource is not a thing it can express, not a thing it has been told not to do.

Adding a kind to that set is a security decision rather than a feature decision, and the enum carries a comment saying so.

### 2. Acting on the control plane

**Failure.** The agent "fixes" CoreDNS, the ingress controller, or Argo CD itself, and removes the machinery you would use to recover.

**Response — prevented.** Twelve namespaces are refused outright, not guarded by a threshold: `kube-system`, `kube-public`, `kube-node-lease`, `cert-manager`, `ingress-nginx`, `istio-system`, `linkerd`, `argocd`, `flux-system`, `monitoring`, `observability`, `velero`.

This is a hard refusal that no autonomy level overrides. On the shipped fixture, the identical `rollback` action is applied on `payments/checkout` and refused on `kube-system/coredns`, purely on namespace. Violation code: `protected_namespace`.

### 3. Irreversible change

**Failure.** The agent makes a change that cannot be mechanically undone, and the undo becomes a judgement call during an incident.

**Response — prevented by construction.** Every action carries `before` and `after`, so its inverse is a field swap. An action whose `before` could not be captured is refused *up front* (`irreversible`) rather than discovered to be irreversible at rollback time.

The stronger version of this property comes from routing through git: undoing the agent is `git revert`, which is not a feature anyone had to build.

### 4. Blast radius larger than intended

**Failure.** A plan that looks small touches forty pods across three namespaces.

**Response — prevented.** Every action declares the pods it disrupts, so the plan is measured while it is still text. Limits apply **to the plan as a whole**, since three individually harmless restarts are not a harmless change. Codes: `blast_radius_pods`, `blast_radius_workloads`, `blast_radius_namespaces`. Under the conservative policy the ceilings are 5 pods, 1 workload, 1 namespace.

### 5. The runaway loop

**Failure.** The agent fixes, breaks, fixes, breaks — or keeps remediating a workload whose real problem is a bug, converting one incident into fifty commits.

**Response — mitigated, two ways.**

*Rate limiting.* A cluster that has already needed several fixes this window is a human's problem, not a loop's (`rate_limited`; conservative policy allows 3 plans per window).

*Visibility.* The incident log surfaces repeat offenders, so a workload remediated weekly stops looking like a weekly success. This is detection rather than prevention — it tells a human that a rollback is papering over a bug, and it is a human who has to act on that.

### 6. A fix that does not work

**Failure.** The agent commits a change that does not help, and walks away satisfied because it stopped at "committed".

**Response — prevented for the observable case.** After committing, kubemend polls the workload. Recovery means the motivating findings are gone **and** no new critical finding appeared, confirmed by **two consecutive clean reads**, because a rollout looks briefly healthy as it begins. On positive evidence of continued failure it withdraws its own commit with `git revert`.

Demonstrated live: a rollback against two consecutive bad releases was still failing after 76s and was reverted automatically, leaving the repository in the state a human last approved.

### 7. Reverting on no evidence

**Failure.** The API server is briefly unreachable, and the agent "helpfully" undoes a fix that was working — turning a network blip into a second production change.

**Response — prevented.** Only `still_failing` triggers a revert. `indeterminate` does not. A poll that cannot reach the cluster proves nothing, so it never counts toward recovery *and* it breaks the recovery streak rather than being skipped — two clean reads either side of a blind one are not two consecutive observations.

**Silence is not success, but it is not failure either.** An unverifiable outcome stops and asks for a human.

### 8. Discarding a human's change

**Failure.** Someone edited the manifest an hour ago; the agent overwrites it based on cluster state that predates the edit.

**Response — prevented.** If the repository's value differs from what the cluster reported, emission refuses rather than editing a stale value. Somebody changed it, and their change is not the agent's to discard. A dirty working tree blocks emission entirely, so an agent's commit never absorbs somebody's in-progress edit.

### 9. Inventing configuration

**Failure.** The agent adds a memory limit, a probe, or a field the author never wrote, and now the manifest asserts something nobody decided.

**Response — prevented.** A field that does not exist is not created. Adjusting a value that is there is a different class of change from inventing one that is not. Similarly, a workload with no manifest is reported, never created.

### 10. Unreviewable changes

**Failure.** The change is technically correct and practically unreviewable, so review becomes rubber-stamping and the git-based safety story becomes theatre.

**Response — prevented by construction.** Manifest edits are surgical text operations against an indentation-path tracker, not parse-and-serialise. A YAML round-trip would rewrite the document, drop comments, reorder keys and normalise quoting — three hundred changed lines for a one-number change. kubemend's diffs are one line, with trailing comments and their exact spacing preserved.

Every commit also carries the evidence that produced it, the blast radius, what was deliberately left alone, and the line `Undo: git revert this commit.`

### 11. Hostile strings from cluster state

**Failure.** Pod names, image references, container names and Kubernetes status messages are attacker-influenceable. They flow into commit messages, manifest lookups and the console.

**Response — partial, and worth stating plainly.**

*What holds.* Manifest targeting is by structured metadata (namespace, kind, name, container) rather than by string interpolation into a path, so a hostile name cannot redirect an edit to another file. Every cluster-derived string the console interpolates into HTML is escaped, and it is placed in element text or double-quoted attributes. The action set is closed, so no string reaches a shell as a command. Journal writes are parameterised SQL; the one interpolated statement is `PRAGMA user_version` with an integer constant.

*What does not.* Evidence strings from cluster state are embedded verbatim in commit messages. A crafted status message could therefore place chosen text in a commit body a human will read. This is a social-engineering surface, not an execution one — it cannot cause an action, only mislead a reviewer about why one happened.

*Not attempted.* There is no sanitisation of these strings today, and no length bound on them.

### 12. Autonomy granted too broadly

**Failure.** A team enables `apply` for everything on day one and discovers the consequences later.

**Response — mitigated by defaults, and by measurement.**

An unconfigured `Policy()` permits nothing; a permissive default here is the difference between a bounded assistant and an unsupervised process with cluster credentials. Under the shipped conservative policy only `rollback` may apply unattended, because it moves a workload to a state that demonstrably ran in this cluster and its inverse is exact.

A plan's autonomy is the **minimum across its actions**, so one action requiring review holds back the whole plan.

The measurement half matters as much: the incident log reports the agent's **revert rate** — how often its own fix failed to hold. That is the honest input to "should this be allowed to apply rather than propose?", and a tool that declined to measure it would be asking for trust it had not earned.

## What the gate enforces

Six properties, checked by a pure function with no model in the loop:

| # | Property | Violation code |
|---|---|---|
| 1 | Protected namespaces refused outright | `protected_namespace` |
| 2 | Only action kinds named in policy; deny by default | `action_not_allowed` |
| 3 | Every action has a computable undo | `irreversible` |
| 4 | Blast radius bounded on pods, workloads, namespaces | `blast_radius_*` |
| 5 | Rate limit per window | `rate_limited` |
| 6 | Autonomy ceiling per action kind | `autonomy_ceiling` |

Policy is **data, not code**, so the rules governing an autonomous system are themselves reviewable, diffable and revertible — the same property the agent's actions are required to have.

## Known limits of this threat model

These are real and should be read before trusting the design further than it deserves.

- **The reconciler is assumed correct and assumed present.** kubemend's contract ends at the commit. If nothing reconciles, nothing happens; if the reconciler misapplies, that is outside the model.
- **Verification is single-workload.** It confirms the treated workload recovered. It does not confirm the change was harmless to dependents, and a fix that repairs one service while breaking its downstream reads as `recovered`.
- **No authentication on the console.** It is read-only and binds to localhost, and that is the entire access control story. Exposing it with `--host` puts an inventory of what is fragile in your cluster on the network.
- **The journal is not tamper-evident.** It is a local SQLite file with no signing or append-only enforcement below the application layer.
- **Detection is pod- and workload-level.** Node, network and storage signals are absent, so a cluster-level cause may be treated as a workload-level symptom — and rolling back a workload whose node is failing is a wasted change, though a reversible one.
- **`Deployment` only.** StatefulSets, DaemonSets and Jobs are unhandled. A StatefulSet is where an irreversible mistake would actually hurt, and it is deliberately not in scope yet.
- **The planner is deterministic.** There is no model in the decision path. That removes prompt injection from the decision loop entirely today, and reintroducing it is the main risk to manage when a model is added — which is why the model is scheduled to correlate and explain rather than to decide.

## On the roadmap

- Sanitising and bounding evidence strings that reach commit messages.
- Dependency-aware verification, so recovery means the neighbourhood recovered.
- Earned autonomy: letting a workload's measured track record raise or lower its ceiling, instead of a static policy file.

## How this compares to the alternative

The honest summary is that kubemend does not make an autonomous agent safe. It makes an autonomous agent's mistakes **bounded, visible and mechanically reversible**, and then measures how often it makes them.

That is a weaker claim than "it will not do anything bad", and it is the strongest claim anyone can currently support.

---

Further reading: [`EVALUATION.md`](EVALUATION.md) for what was measured, [`FINDINGS.md`](FINDINGS.md) for what the design work turned up, and [`kubemend-report.pdf`](kubemend-report.pdf) for the full write-up.
