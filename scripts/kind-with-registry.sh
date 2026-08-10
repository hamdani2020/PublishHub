#!/usr/bin/env bash
# scripts/kind-with-registry.sh
#
# Create (or reuse) a local Docker registry and a kind cluster configured to
# pull from it. The script is idempotent: running it against an existing setup
# produces a success message without duplication or error.
#
# Configuration (environment variables, defaults match the Makefile):
#   CLUSTER_NAME   — kind cluster name          (default: publishhub-cluster)
#   REGISTRY_NAME  — Docker container name      (default: publishhub-registry)
#   REGISTRY_PORT  — host port for the registry (default: 5001)
#
# Requirements satisfied: 1.1, 1.2, 1.3, 1.4

set -euo pipefail

# --- Configuration -----------------------------------------------------------

CLUSTER_NAME="${CLUSTER_NAME:-publishhub-cluster}"
REGISTRY_NAME="${REGISTRY_NAME:-publishhub-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5001}"

# --- Helper functions --------------------------------------------------------

info() { printf '  [info]  %s\n' "$*"; }
ok()   { printf '  [ok]    %s\n' "$*"; }
err()  { printf '  [error] %s\n' "$*" >&2; }

# --- 1. Ensure the local registry container ----------------------------------

registry_running() {
  docker inspect --format '{{.State.Running}}' "${REGISTRY_NAME}" 2>/dev/null | grep -q '^true$'
}

registry_exists() {
  docker inspect "${REGISTRY_NAME}" >/dev/null 2>&1
}

if registry_running; then
  ok "Registry '${REGISTRY_NAME}' already running on localhost:${REGISTRY_PORT}"
elif registry_exists; then
  info "Registry container exists but is stopped — starting it"
  docker start "${REGISTRY_NAME}" >/dev/null
  ok "Registry '${REGISTRY_NAME}' started on localhost:${REGISTRY_PORT}"
else
  info "Creating registry container '${REGISTRY_NAME}' on port ${REGISTRY_PORT}"
  docker run \
    --detach \
    --restart always \
    --name "${REGISTRY_NAME}" \
    --publish "127.0.0.1:${REGISTRY_PORT}:5000" \
    registry:2 >/dev/null
  ok "Registry '${REGISTRY_NAME}' created on localhost:${REGISTRY_PORT}"
fi

# --- 2. Create the kind cluster (if it does not exist) -----------------------

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  ok "Kind cluster '${CLUSTER_NAME}' already exists"
else
  info "Creating kind cluster '${CLUSTER_NAME}' (1 control-plane + 2 workers)"

  # The containerdConfigPatches tell each node to resolve localhost:<port> from
  # the registry container rather than trying to reach a registry on the node's
  # own loopback (Requirement 1.2).
  cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors."localhost:${REGISTRY_PORT}"]
      endpoint = ["http://${REGISTRY_NAME}:5000"]
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
  ok "Kind cluster '${CLUSTER_NAME}' created"
fi

# --- 3. Connect the registry to the kind network ----------------------------

# kind uses a Docker network named "kind" by default. If the registry is not
# already attached, connect it so that in-cluster containerd can reach it by
# container name.

if docker network inspect kind -f '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | grep -q "${REGISTRY_NAME}"; then
  ok "Registry already connected to the 'kind' network"
else
  info "Connecting registry to the 'kind' Docker network"
  docker network connect kind "${REGISTRY_NAME}" 2>/dev/null || true
  ok "Registry connected to the 'kind' network"
fi

# --- 4. Document the local registry via a ConfigMap --------------------------
# This follows the kind documentation convention so tools that understand the
# local-registry protocol can discover it.

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REGISTRY_PORT}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

# --- 5. Readiness message (Requirement 1.4) ----------------------------------

printf '\n'
printf '  ============================================================\n'
printf '  PublishHub local Kubernetes environment is ready.\n'
printf '\n'
printf '    cluster:   %s\n' "${CLUSTER_NAME}"
printf '    registry:  localhost:%s\n' "${REGISTRY_PORT}"
printf '    nodes:     1 control-plane, 2 workers\n'
printf '\n'
printf '  Push images with:\n'
printf '    docker tag <image> localhost:%s/<image>\n' "${REGISTRY_PORT}"
printf '    docker push localhost:%s/<image>\n' "${REGISTRY_PORT}"
printf '  ============================================================\n'
printf '\n'
