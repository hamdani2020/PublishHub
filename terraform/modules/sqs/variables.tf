variable "project" {
  description = "Project name used as a prefix for resource names."
  type        = string
  default     = "publishhub"
}

variable "environment" {
  description = "Deployment environment (e.g. production, staging)."
  type        = string
}

variable "message_retention_seconds" {
  description = "How long messages stay in the main queue before being deleted."
  type        = number
  default     = 345600 # 4 days
}

variable "visibility_timeout_seconds" {
  description = "How long a received message is hidden from other consumers."
  type        = number
  default     = 60
}

variable "receive_wait_time_seconds" {
  description = "Long-poll wait time for ReceiveMessage calls."
  type        = number
  default     = 20
}

variable "max_receive_count" {
  description = "Number of receives before a message is sent to the DLQ."
  type        = number
  default     = 3
}

variable "dlq_message_retention_seconds" {
  description = "How long messages stay in the dead-letter queue."
  type        = number
  default     = 1209600 # 14 days
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
