variable "project" {
  description = "Project name."
  type        = string
}

variable "environment" {
  description = "Deployment environment."
  type        = string
}

variable "cluster_name" {
  description = "Name of the EKS cluster (for kubeconfig generation)."
  type        = string
}

variable "repo_url" {
  description = "Git repository URL for the Helm chart source."
  type        = string
}

variable "target_revision" {
  description = "Git revision (branch, tag, or commit) ArgoCD should track."
  type        = string
  default     = "HEAD"
}

variable "chart_path" {
  description = "Path to the Helm chart within the repository."
  type        = string
  default     = "helm/publishhub"
}

variable "destination_namespace" {
  description = "Kubernetes namespace to deploy into."
  type        = string
  default     = "publishhub"
}

variable "argocd_namespace" {
  description = "Namespace where ArgoCD is installed."
  type        = string
  default     = "argocd"
}

variable "ecr_registry" {
  description = "ECR registry URL (account_id.dkr.ecr.region.amazonaws.com)."
  type        = string
}

variable "sqs_queue_url" {
  description = "URL of the SQS job queue."
  type        = string
}

variable "sqs_region" {
  description = "AWS region for SQS."
  type        = string
}

variable "irsa_role_arn" {
  description = "ARN of the IAM role for the service account (IRSA)."
  type        = string
}
