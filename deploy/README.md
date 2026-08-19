# deploy

A real deployment: a cluster, **actual Argo CD**, and a **GitOps repository on
GitHub** that Argo CD reconciles. kubemend is given write access to that
repository and nothing else.

```bash
deploy/up.sh                    # cluster + Argo CD + point it at the repo
deploy/incident.sh              # ship a bad release; let the agent handle it
deploy/incident.sh --bad-twice  # two bad releases, so the rollback also fails
deploy/down.sh                  # tear the cluster down
```

Needs `k3d`, `kubectl`, an authenticated `gh`, and a Docker daemon. Argo CD
wants roughly 4 GB for the VM; `colima start --cpu 4 --memory 4` is enough.

## Why this exists separately from `demo/`

[`demo/run.sh`](../demo/run.sh) substitutes `kubectl apply` for a reconciler and
uses a local repository with no remote. That is a reasonable demo and it leaves
two claims untested:

1. that a real reconciler picks up the agent's commits, and
2. that the pull-request path works against a real forge.

**Running this found a bug the substitute had been hiding.** With `kubectl
apply` reading the working tree, a local commit *was* the delivery — so `apply`
committed and never pushed, and nothing noticed. Against Argo CD watching
GitHub the commit is invisible: the workload never recovers, verification
reports `still_failing`, and the agent reverts a fix that was correct but
undelivered. Thirty-three tests covered the emission path and none had a remote.

That is written up as Finding 7 in [`../docs/FINDINGS.md`](../docs/FINDINGS.md),
and the failure mode is catalogued as #13 in
[`../docs/THREAT-MODEL.md`](../docs/THREAT-MODEL.md).

## What you can check afterwards

- **The GitOps repository** — [github.com/Srivatsa03/kubemend-gitops-demo](https://github.com/Srivatsa03/kubemend-gitops-demo).
  Commits authored by `kubemend` were written by the agent, unattended. A
  `Revert "..."` commit means it withdrew its own change after watching.
- **Argo CD** — `kubectl -n argocd port-forward svc/argocd-server 8080:443`,
  then log in with the password from
  `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`.
- **The agent's own record** — `kubemend log --journal /tmp/kubemend-live-journal.db`.

## Honest scope

This is a real cluster, a real reconciler and a real forge. It is **not** a
production deployment: the cluster is local, there is no traffic, and the
incidents are injected rather than organic. What it establishes is that the loop
closes against the real dependencies rather than against a stand-in.
