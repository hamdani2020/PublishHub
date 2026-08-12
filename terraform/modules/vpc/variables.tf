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

variable "aws_region" {
  description = "AWS region for data source lookups"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to use"
  type        = number
  default     = 3
}

variable "single_nat_gateway" {
  description = "Use a single NAT gateway for cost optimization (true) or one per AZ for HA (false)"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
