# Manages the ArgoCD Application resource via kubectl.
# This injects account-specific Helm parameters (ECR repos, SQS URL, IRSA ARN)
# so they never need to live in a public git repository.


locals {
  app_project_manifest = yamlencode({
    apiVersion = "argoproj.io/v1alpha1"
    kind       = "AppProject"

    metadata = {
      name      = var.project
      namespace = var.argocd_namespace
    }

    spec = {
      description = "${var.project} application project"

      sourceRepos = [var.repo_url]

      destinations = [
        {
          server    = "https://kubernetes.default.svc"
          namespace = var.destination_namespace
        }
      ]

      clusterResourceWhitelist = [
        { group = "", kind = "Namespace" },
        { group = "rbac.authorization.k8s.io", kind = "ClusterRole" },
        { group = "rbac.authorization.k8s.io", kind = "ClusterRoleBinding" },
      ]

      namespaceResourceWhitelist = [
        { group = "*", kind = "*" }
      ]

      orphanedResources = {
        warn = true
      }
    }
  })

  app_manifest = yamlencode({
    apiVersion = "argoproj.io/v1alpha1"
    kind       = "Application"

    metadata = {
      name      = var.project
      namespace = var.argocd_namespace
      finalizers = [
        "resources-finalizer.argocd.argoproj.io"
      ]
    }

    spec = {
      project = var.project

      source = {
        repoURL        = var.repo_url
        targetRevision = var.target_revision
        path           = var.chart_path

        helm = {
          valueFiles = [
            "values.yaml",
            "values-production.yaml",
          ]

          parameters = [
            {
              name  = "api.image.repository"
              value = "${var.ecr_registry}/publishhub-api"
            },
            {
              name  = "worker.image.repository"
              value = "${var.ecr_registry}/publishhub-worker"
            },
            {
              name  = "web.image.repository"
              value = "${var.ecr_registry}/publishhub-web"
            },
            {
              name  = "sqs.queueUrl"
              value = var.sqs_queue_url
            },
            {
              name  = "sqs.region"
              value = var.sqs_region
            },
            {
              name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
              value = var.irsa_role_arn
            },
          ]
        }
      }

      destination = {
        server    = "https://kubernetes.default.svc"
        namespace = var.destination_namespace
      }

      syncPolicy = {
        automated = {
          prune    = true
          selfHeal = true
        }
        syncOptions = [
          "CreateNamespace=true",
          "ServerSideApply=true",
        ]
        retry = {
          limit = 3
          backoff = {
            duration    = "5s"
            factor      = 2
            maxDuration = "1m"
          }
        }
      }
    }
  })
}

resource "null_resource" "apply_argocd_application" {
  triggers = {
    manifest_sha = sha256("${local.app_project_manifest}${local.app_manifest}")
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws eks update-kubeconfig \
        --name ${var.cluster_name} \
        --region ${var.sqs_region} \
        --kubeconfig /tmp/publishhub-kubeconfig

      cat > /tmp/publishhub-argocd-project.yaml <<'MANIFEST'
${local.app_project_manifest}
MANIFEST

      cat > /tmp/publishhub-argocd-app.yaml <<'MANIFEST'
${local.app_manifest}
MANIFEST

      kubectl apply \
        --kubeconfig /tmp/publishhub-kubeconfig \
        -f /tmp/publishhub-argocd-project.yaml

      kubectl apply \
        --kubeconfig /tmp/publishhub-kubeconfig \
        -f /tmp/publishhub-argocd-app.yaml

      rm -f /tmp/publishhub-kubeconfig /tmp/publishhub-argocd-project.yaml /tmp/publishhub-argocd-app.yaml
    EOT
  }
}
