variable "project" {
  description = "Project name used as a prefix for role names."
  type        = string
  default     = "publishhub"
}

variable "environment" {
  description = "Deployment environment (e.g. production, staging)."
  type        = string
}

# --- EKS OIDC (IRSA) ---

variable "eks_oidc_issuer_url" {
  description = "OIDC issuer URL from the EKS cluster (e.g. https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE)."
  type        = string
}

variable "eks_oidc_provider_arn" {
  description = "ARN of the EKS OIDC provider (passed directly to avoid data source race conditions)."
  type        = string
}


# --- KEDA ---

variable "keda_namespace" {
  description = "Kubernetes namespace where KEDA is installed."
  type        = string
  default     = "keda"
}

variable "keda_service_account" {
  description = "Service account name KEDA uses to assume the IRSA role."
  type        = string
  default     = "keda-operator"
}

# --- Worker ---

variable "app_namespace" {
  description = "Kubernetes namespace where the application workloads run."
  type        = string
  default     = "publishhub"
}

variable "worker_service_account" {
  description = "Service account name the worker pods use."
  type        = string
  default     = "publishhub-worker"
}

# --- SQS ARNs ---

variable "sqs_queue_arns" {
  description = "List of SQS queue ARNs (main + DLQ) that KEDA and the worker need access to."
  type        = list(string)
}

# --- ECR ARNs ---

variable "ecr_repository_arns" {
  description = "List of ECR repository ARNs that the GitHub Actions role can push to."
  type        = list(string)
}

# --- GitHub OIDC ---

variable "create_github_oidc_provider" {
  description = "Whether to create the GitHub OIDC provider (set to false if it already exists in the account)."
  type        = bool
  default     = true
}

variable "github_oidc_thumbprint" {
  description = "TLS certificate thumbprint for the GitHub OIDC provider. AWS no longer validates this for GitHub but it is still required by the API."
  type        = string
  default     = "1c58a3a8518e8759bf075b76b750d4f2df264fcd"
}

variable "github_repository" {
  description = "GitHub repository in the form owner/repo that is allowed to assume the role."
  type        = string
}

variable "github_owner_id" {
  description = "Immutable numeric ID of the GitHub owner (user or org). Required for repos created after July 15 2026. Get with: gh api users/OWNER --jq '.id'"
  type        = string
  default     = ""
}

variable "github_repo_id" {
  description = "Immutable numeric ID of the GitHub repository. Required for repos created after July 15 2026. Get with: gh api repos/OWNER/REPO --jq '.id'"
  type        = string
  default     = ""
}

# --- Tags ---

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
