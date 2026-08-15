#!/usr/bin/env bash
# A live demonstration, end to end, on a throwaway cluster.
#
#   1. Stand up a k3d cluster and a git repository holding its manifests.
#   2. Apply the repository, the way Argo CD or Flux would.
#   3. Ship a release that breaks, the way a release does.
#   4. Let kubemend read the cluster, write a fix to the repository, and
#      reconcile it back.
#
# Nothing here is scripted around kubemend: it reads the real cluster with
# read-only verbs and its only write is a git commit. The `kubectl apply` after
# it stands in for the reconciler you would already be running.
#
# Usage:  demo/run.sh [--keep]      (--keep leaves the cluster up afterwards)

set -euo pipefail

CLUSTER=kubemend-demo
REPO="${KUBEMEND_DEMO_REPO:-/tmp/kubemend-gitops}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBEMEND="${KUBEMEND_BIN:-$HERE/.venv/bin/kubemend}"
KEEP=${1:-}

blue() { printf "\033[36;1m\n== %s\033[0m\n" "$1"; }
dim()  { printf "\033[2m%s\033[0m\n" "$1"; }

cleanup() {
  if [ "$KEEP" != "--keep" ]; then
    blue "Tearing down"
    k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
    dim "cluster deleted; pass --keep to leave it running"
  else
    dim "cluster $CLUSTER left running; k3d cluster delete $CLUSTER to remove"
  fi
}
trap cleanup EXIT

# --- 1. cluster ---------------------------------------------------------------

blue "1. A cluster"
if ! k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster create "$CLUSTER" --agents 1 --wait >/dev/null 2>&1
fi
kubectl config use-context "k3d-$CLUSTER" >/dev/null
kubectl get nodes --no-headers | awk '{print "   " $1 "  " $2}'

# --- 2. the repository that defines it ----------------------------------------

blue "2. A git repository holding the desired state"
rm -rf "$REPO" && mkdir -p "$REPO/clusters/prod"
git -C "$REPO" init -q -b main
git -C "$REPO" config user.email demo@kubemend
git -C "$REPO" config user.name kubemend-demo

cat > "$REPO/clusters/prod/checkout.yaml" <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: payments
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: payments
spec:
  replicas: 2
  selector:
    matchLabels:
      app: checkout
  template:
    metadata:
      labels:
        app: checkout
    spec:
      containers:
        - name: checkout
          image: nginx:1.27-alpine   # known good
          ports:
            - containerPort: 80
          resources:
            limits:
              memory: 64Mi
YAML

git -C "$REPO" add -A
git -C "$REPO" commit -q -m "checkout on nginx:1.27-alpine"
kubectl apply -f "$REPO/clusters/prod/" >/dev/null
kubectl -n payments rollout status deploy/checkout --timeout=90s | sed 's/^/   /'

# --- 3. a release that breaks -------------------------------------------------

blue "3. A release goes out, and it is wrong"
sed -i.bak 's|nginx:1.27-alpine|nginx:1.27-alpine-typo|' "$REPO/clusters/prod/checkout.yaml"
rm -f "$REPO/clusters/prod/checkout.yaml.bak"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "bump checkout image"
kubectl apply -f "$REPO/clusters/prod/" >/dev/null
dim "   waiting for the cluster to notice..."
sleep 25
kubectl -n payments get pods --no-headers | awk '{print "   " $1 "  " $3}'

# --- 4. kubemend reads, and writes a commit -----------------------------------

blue "4. kubemend reads the cluster (read-only)"
# Show the findings and the gated plan, not just the banner.
"$KUBEMEND" diagnose -n payments --no-color | sed '/^$/d;s/^/  /' | head -16

blue "5. kubemend writes the fix to the repository"
"$KUBEMEND" remediate -n payments --repo "$REPO" --no-color | sed '/^$/d;s/^/  /' 

blue "6. The commit it produced"
git -C "$REPO" log -1 --stat | sed 's/^/   /'

# --- 5. the reconciler applies it ---------------------------------------------

blue "7. The reconciler applies the repository, and the cluster recovers"
kubectl apply -f "$REPO/clusters/prod/" >/dev/null
kubectl -n payments rollout status deploy/checkout --timeout=90s | sed 's/^/   /'
kubectl -n payments get pods --no-headers | awk '{print "   " $1 "  " $3}'

blue "Done"
dim "The agent never called the Kubernetes API to change anything."
dim "Undo the whole thing with: git -C $REPO revert HEAD"
