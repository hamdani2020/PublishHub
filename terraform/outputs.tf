output "cluster_name" {
  description = "Name of the EKS cluster."
  value       = module.eks.cluster_name
}

output "aws_region" {
  description = "AWS region where resources are deployed."
  value       = var.aws_region
}

output "ecr_repository_urls" {
  description = "Map of service name to ECR repository URL."
  value       = module.ecr.repository_urls
}

output "sqs_queue_url" {
  description = "URL of the main SQS job queue."
  value       = module.sqs.queue_url
}

output "sqs_dlq_url" {
  description = "URL of the dead-letter queue."
  value       = module.sqs.dlq_url
}

output "cluster_endpoint" {
  description = "Endpoint for the EKS cluster API server."
  value       = module.eks.cluster_endpoint
}

output "github_actions_role_arn" {
  description = "ARN of the IAM role for GitHub Actions OIDC authentication."
  value       = module.iam.github_actions_role_arn
}

output "worker_role_arn" {
  description = "ARN of the IAM role for the worker service account (IRSA)."
  value       = module.iam.worker_role_arn
}

output "ecr_registry" {
  description = "ECR registry URL (account_id.dkr.ecr.region.amazonaws.com)."
  value       = "${module.ecr.registry_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}
