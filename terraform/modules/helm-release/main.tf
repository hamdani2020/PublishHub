# Installs a Helm chart via the helm CLI at apply time.
#
# This avoids configuring the helm/kubernetes Terraform providers, which would
# otherwise need the EKS cluster endpoint to be known at plan time. On a cold
# apply (cluster does not exist yet) that plan-time dependency fails with
# "Kubernetes cluster unreachable". Running the CLI at apply time — after the
# cluster is up — sidesteps the problem and keeps a single `terraform apply`
# working from zero. This mirrors the argocd-app module convention.

locals {
  # Deterministic --set flags, sorted so the trigger hash is stable.
  set_flags = join(" ", [
    for k in sort(keys(var.values)) :
    format("--set %s=%s", k, replace(var.values[k], ",", "\\,"))
  ])
}

resource "null_resource" "release" {
  triggers = {
    release_name  = var.release_name
    chart         = var.chart
    chart_version = var.chart_version
    namespace     = var.namespace
    repository    = var.repository
    cluster_name  = var.cluster_name
    aws_region    = var.aws_region
    values_sha    = sha256(jsonencode(var.values))

    # Computed cluster attribute, only known after the cluster is created.
    # This forces the provisioner to run after cluster creation completes,
    # which cluster_name (a static string) does not guarantee on its own.
    cluster_endpoint = var.cluster_endpoint
  }

  # Install / upgrade.
  provisioner "local-exec" {
    command = <<-EOT
      set -euo pipefail

      KUBECONFIG_FILE="$(mktemp)"
      trap 'rm -f "$KUBECONFIG_FILE"' EXIT

      export AWS_PROFILE=lusilearn

      aws eks update-kubeconfig \
        --name ${var.cluster_name} \
        --region ${var.aws_region} \
        --kubeconfig "$KUBECONFIG_FILE"

      export KUBECONFIG="$KUBECONFIG_FILE"

      # Wait for worker nodes to register and report Ready before installing.
      # aws_eks_node_group reporting ACTIVE does not guarantee kubelets have
      # joined the cluster, so `helm --wait` could otherwise fail scheduling
      # pods. Poll until at least the expected number of nodes are Ready.
      echo "Waiting for at least ${var.min_ready_nodes} node(s) to be Ready (timeout ${var.node_ready_timeout}s)..."
      deadline=$(( $(date +%s) + ${var.node_ready_timeout} ))
      while true; do
        ready=$(kubectl get nodes \
          --no-headers 2>/dev/null \
          | grep -cw "Ready" || true)
        if [ "$ready" -ge "${var.min_ready_nodes}" ]; then
          echo "$ready node(s) Ready."
          break
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
          echo "Timed out waiting for nodes to be Ready ($ready ready)." >&2
          exit 1
        fi
        echo "  $ready/${var.min_ready_nodes} nodes Ready; retrying in 10s..."
        sleep 10
      done

      # Belt-and-suspenders: block on the node Ready condition too, so we don't
      # proceed while a node is registered but still initializing.
      kubectl wait --for=condition=Ready nodes --all \
        --timeout=${var.node_ready_timeout}s

      helm repo add ${var.release_name} ${var.repository}
      helm repo update ${var.release_name}

      helm upgrade --install ${var.release_name} ${var.release_name}/${var.chart} \
        --version ${var.chart_version} \
        --namespace ${var.namespace} \
        --create-namespace \
        --wait \
        --timeout ${var.timeout}s \
        ${local.set_flags}
    EOT
  }

  # Uninstall on destroy. Values captured via self/triggers because var.* is not
  # available in destroy-time provisioners.
  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      set -euo pipefail

      KUBECONFIG_FILE="$(mktemp)"
      trap 'rm -f "$KUBECONFIG_FILE"' EXIT

      aws eks update-kubeconfig \
        --name ${self.triggers.cluster_name} \
        --region ${self.triggers.aws_region} \
        --kubeconfig "$KUBECONFIG_FILE" || exit 0

      export KUBECONFIG="$KUBECONFIG_FILE"

      helm uninstall ${self.triggers.release_name} \
        --namespace ${self.triggers.namespace} \
        --ignore-not-found || true
    EOT

    on_failure = continue
  }
}
