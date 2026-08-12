variable "project" {
  description = "Project name used for resource naming and tagging"
  type        = string
  default     = "publishhub"
}

variable "environment" {
  description = "Environment name (e.g. production, staging)"
  type        = string
  default     = "production"
}

variable "cluster_version" {
  description = "Kubernetes version for the EKS cluster"
  type        = string
  default     = "1.29"
}

variable "vpc_id" {
  description = "VPC ID where the EKS cluster will be created"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the EKS cluster and node groups"
  type        = list(string)
}

variable "node_group_instance_types" {
  description = "Instance types for the managed node group (ARM types for cost optimization)"
  type        = list(string)
  default     = ["t4g.medium", "t4g.large", "m7g.medium", "m7g.large"]
}

variable "node_group_capacity_type" {
  description = "Capacity type for the managed node group: ON_DEMAND or SPOT"
  type        = string
  default     = "SPOT"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_group_capacity_type)
    error_message = "capacity_type must be ON_DEMAND or SPOT"
  }
}

variable "node_group_desired_size" {
  description = "Desired number of nodes in the managed node group"
  type        = number
  default     = 2
}

variable "node_group_min_size" {
  description = "Minimum number of nodes in the managed node group"
  type        = number
  default     = 1
}

variable "node_group_max_size" {
  description = "Maximum number of nodes in the managed node group"
  type        = number
  default     = 5
}

variable "node_group_disk_size" {
  description = "Disk size in GB for the node group instances"
  type        = number
  default     = 20
}

variable "node_group_ami_type" {
  description = "AMI type for the node group (AL2_ARM_64 for ARM instances)"
  type        = string
  default     = "AL2_ARM_64"
}

variable "cluster_endpoint_public_access" {
  description = "Whether the EKS cluster API endpoint is publicly accessible"
  type        = bool
  default     = true
}

variable "cluster_endpoint_private_access" {
  description = "Whether the EKS cluster API endpoint is privately accessible"
  type        = bool
  default     = true
}

variable "enabled_cluster_log_types" {
  description = "List of EKS cluster log types to enable"
  type        = list(string)
  default     = ["api", "audit", "authenticator"]
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
