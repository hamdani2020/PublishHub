variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used for resource naming and tagging."
  type        = string
  default     = "publishhub"
}

variable "environment" {
  description = "Deployment environment (e.g. production, staging)."
  type        = string
  default     = "production"
}

# --- VPC ---

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to use."
  type        = number
  default     = 3
}

variable "single_nat_gateway" {
  description = "Use a single NAT gateway for cost optimization (true) or one per AZ for HA (false)."
  type        = bool
  default     = true
}

# --- EKS ---

variable "cluster_version" {
  description = "Kubernetes version for the EKS cluster."
  type        = string
  default     = "1.29"
}

variable "node_group_instance_types" {
  description = "Instance types for the managed node group (ARM types for cost optimization)."
  type        = list(string)
  default     = ["t4g.medium", "t4g.large", "m7g.medium", "m7g.large"]
}

variable "node_group_capacity_type" {
  description = "Capacity type for the managed node group: ON_DEMAND or SPOT."
  type        = string
  default     = "SPOT"
}

variable "node_group_desired_size" {
  description = "Desired number of nodes in the managed node group."
  type        = number
  default     = 2
}

variable "node_group_min_size" {
  description = "Minimum number of nodes in the managed node group."
  type        = number
  default     = 1
}

variable "node_group_max_size" {
  description = "Maximum number of nodes in the managed node group."
  type        = number
  default     = 5
}

# --- IAM / GitHub OIDC ---

variable "github_repository" {
  description = "GitHub repository in the form owner/repo that is allowed to assume the deploy role."
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

variable "create_github_oidc_provider" {
  description = "Whether to create the GitHub OIDC provider (set to false if it already exists in the account)."
  type        = bool
  default     = true
}

# --- Tags ---

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
