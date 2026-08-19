#!/usr/bin/env bash
# Tear down the cluster. The GitOps repository is left alone: its history is
# the record of what the agent did, and that is the point of it.
set -euo pipefail
CLUSTER="${KUBEMEND_CLUSTER:-kubemend-live}"
k3d cluster delete "$CLUSTER"
printf "\033[2m%s\033[0m\n" "cluster deleted; the GitOps repo and its history remain"
