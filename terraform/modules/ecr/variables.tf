variable "project" {
  description = "Project name used as a prefix for repository names."
  type        = string
  default     = "publishhub"
}

variable "environment" {
  description = "Deployment environment (e.g. production, staging)."
  type        = string
}

variable "services" {
  description = "List of service names. One ECR repository is created per service."
  type        = list(string)
  default     = ["api", "worker", "web"]
}

variable "image_tag_mutability" {
  description = "Image tag mutability setting for the repositories."
  type        = string
  default     = "MUTABLE"
}

variable "scan_on_push" {
  description = "Whether to scan images for vulnerabilities on push."
  type        = bool
  default     = true
}

variable "untagged_expiry_days" {
  description = "Number of days after which untagged images expire."
  type        = number
  default     = 1
}

variable "max_tagged_image_count" {
  description = "Maximum number of tagged images to retain per repository."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
