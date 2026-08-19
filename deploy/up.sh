#!/usr/bin/env bash
# Stand up a real deployment: a cluster, Argo CD, and a GitOps repository on
# GitHub that Argo CD reconciles.
#
# This differs from demo/run.sh in the part that matters. The demo substitutes
# `kubectl apply` for a reconciler and uses a local repository with no remote,
# which leaves two claims untested: that a real reconciler picks up the agent's
# commits, and that the pull-request path works against a real forge. Here both
# are real.
#
# Usage:  deploy/up.sh
#
# Requires: k3d, kubectl, gh (authenticated), and a running Docker daemon.

set -euo pipefail

CLUSTER="${KUBEMEND_CLUSTER:-kubemend-live}"
GITOPS_REPO="${KUBEMEND_GITOPS_REPO:-Srivatsa03/kubemend-gitops-demo}"
WORKDIR="${KUBEMEND_WORKDIR:-/tmp/kubemend-live}"

blue() { printf "\033[36;1m\n== %s\033[0m\n" "$1"; }
dim()  { printf "\033[2m%s\033[0m\n" "$1"; }

blue "1. A cluster"
if ! k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster create "$CLUSTER" --agents 1 --wait >/dev/null 2>&1
fi
kubectl config use-context "k3d-$CLUSTER" >/dev/null
kubectl get nodes --no-headers | awk '{print "   " $1 "  " $2}'

blue "2. Argo CD"
if ! kubectl get ns argocd >/dev/null 2>&1; then
  kubectl create namespace argocd >/dev/null
  kubectl apply -n argocd \
    -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml >/dev/null
fi
for d in argocd-repo-server argocd-server; do
  kubectl -n argocd rollout status "deploy/$d" --timeout=420s | sed 's/^/   /'
done
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=420s \
  | sed 's/^/   /'

blue "3. The repository Argo CD will reconcile"
rm -rf "$WORKDIR"
gh repo clone "$GITOPS_REPO" "$WORKDIR" -- --quiet
git -C "$WORKDIR" config user.name  "kubemend"
git -C "$WORKDIR" config user.email "kubemend@users.noreply.github.com"
dim "   $WORKDIR  ->  https://github.com/$GITOPS_REPO"

blue "4. Point Argo CD at it"
kubectl apply -f "$WORKDIR/argocd/application.yaml" >/dev/null
dim "   waiting for the first sync..."
for _ in $(seq 1 60); do
  health=$(kubectl -n argocd get application payments \
    -o jsonpath='{.status.health.status}' 2>/dev/null || true)
  sync=$(kubectl -n argocd get application payments \
    -o jsonpath='{.status.sync.status}' 2>/dev/null || true)
  [ "$health" = "Healthy" ] && [ "$sync" = "Synced" ] && break
  sleep 5
done
printf "   application payments: %s / %s\n" "${sync:-?}" "${health:-?}"
kubectl -n payments get pods --no-headers 2>/dev/null | awk '{print "   " $1 "  " $3}'

blue "Ready"
dim "The cluster now runs whatever is committed at github.com/$GITOPS_REPO."
dim "Nothing else writes to it — including kubemend, which only writes commits."
dim ""
dim "  deploy/incident.sh    ship a release that breaks, and let kubemend fix it"
dim "  deploy/down.sh        tear it all down"
