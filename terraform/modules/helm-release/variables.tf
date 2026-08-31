variable "cluster_name" {
  description = "Name of the EKS cluster (for kubeconfig generation)."
  type        = string
}

variable "cluster_endpoint" {
  description = <<-EOT
    API server endpoint of the EKS cluster. This is a computed attribute that
    is only known after the cluster resource has finished creating. Referencing
    it in the null_resource triggers establishes a real dependency edge so the
    helm install cannot run until the cluster actually exists. (cluster_name
    alone is a plan-time-known static string and creates no such ordering.)
  EOT
  type        = string
}

variable "aws_region" {
  description = "AWS region of the EKS cluster."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
}

variable "repository" {
  description = "Helm chart repository URL."
  type        = string
}

variable "chart" {
  description = "Helm chart name."
  type        = string
}

variable "chart_version" {
  description = "Helm chart version to install."
  type        = string
}

variable "namespace" {
  description = "Namespace to install the release into (created if missing)."
  type        = string
}

variable "values" {
  description = "Map of Helm values to set via --set."
  type        = map(string)
  default     = {}
}

variable "timeout" {
  description = "Timeout for the helm install/upgrade, in seconds."
  type        = number
  default     = 600
}

variable "min_ready_nodes" {
  description = "Minimum number of worker nodes that must report Ready before installing the chart."
  type        = number
  default     = 1
}

variable "node_ready_timeout" {
  description = "How long to wait, in seconds, for worker nodes to become Ready."
  type        = number
  default     = 600
}
