#!/usr/bin/env bash
# Ship a release that breaks, then let kubemend handle it against the real
# deployment: real Argo CD, real GitHub repository, real pull requests.
#
# Nothing here is scripted around the agent. It reads the live cluster with
# read-only verbs, commits to the GitOps repository, and Argo CD — which was
# already running — carries the change back to the cluster on its own.
#
# Usage:  deploy/incident.sh [--bad-twice]
#
#   (default)     one bad release; the rollback should work and be kept
#   --bad-twice   two bad releases in a row, so the rollback lands on another
#                 broken revision and the agent has to withdraw its own commit

set -euo pipefail

WORKDIR="${KUBEMEND_WORKDIR:-/tmp/kubemend-live}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBEMEND="${KUBEMEND_BIN:-$HERE/.venv/bin/kubemend}"
JOURNAL="${KUBEMEND_JOURNAL:-/tmp/kubemend-live-journal.db}"
MODE="${1:-}"

blue() { printf "\033[36;1m\n== %s\033[0m\n" "$1"; }
dim()  { printf "\033[2m%s\033[0m\n" "$1"; }

ship() {  # ship <image> <message>
  sed -i.bak "s|image: nginx:[^ ]*|image: $1|" "$WORKDIR/clusters/prod/checkout.yaml"
  rm -f "$WORKDIR/clusters/prod/checkout.yaml.bak"
  git -C "$WORKDIR" add -A
  git -C "$WORKDIR" -c user.name="Srivatsa Kamballa" \
      -c user.email="ajithr.moola@gmail.com" commit -q -m "$2"
  git -C "$WORKDIR" push -q origin main
}

wait_sync() {
  for _ in $(seq 1 "${1:-40}"); do
    kubectl -n argocd get application payments \
      -o jsonpath='{.status.sync.status}' 2>/dev/null | grep -q Synced && return 0
    sleep 5
  done
}

git -C "$WORKDIR" pull -q --ff-only origin main

blue "1. A release goes out, and it is wrong"
ship "nginx:1.27-alpine-typo" "bump checkout image"
dim "   pushed to GitHub; Argo CD will pick it up on its own"
wait_sync 30
sleep 20
kubectl -n payments get pods --no-headers | awk '{print "   " $1 "  " $3}'

if [ "$MODE" = "--bad-twice" ]; then
  blue "1b. And the release before it was bad too"
  ship "nginx:1.27-broken-b" "bump checkout image again"
  wait_sync 30
  sleep 20
  dim "   rolling back one revision now lands on another broken one"
fi

blue "2. kubemend reads the cluster (read-only)"
"$KUBEMEND" diagnose -n payments --no-color | sed '/^$/d;s/^/  /' | head -12

blue "3. It commits the fix, then watches to see whether it worked"
dim "   no kubectl apply here — Argo CD is the only thing that touches the cluster"
"$KUBEMEND" remediate -n payments --repo "$WORKDIR" --verify --verify-timeout 180 \
  --pr --journal "$JOURNAL" --no-color | sed '/^$/d;s/^/  /'

blue "4. What landed on GitHub"
git -C "$WORKDIR" log --oneline -4 | sed 's/^/   /'

blue "5. The cluster, afterwards"
kubectl -n argocd get application payments \
  -o jsonpath='   sync={.status.sync.status}  health={.status.health.status}{"\n"}'
kubectl -n payments get pods --no-headers | awk '{print "   " $1 "  " $3}'

blue "6. What it knows about itself"
"$KUBEMEND" log --journal "$JOURNAL" --no-color | sed '/^$/d;s/^/  /'
