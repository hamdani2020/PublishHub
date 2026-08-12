output "repository_urls" {
  description = "Map of service name to ECR repository URL."
  value       = { for k, v in aws_ecr_repository.service : k => v.repository_url }
}

output "repository_arns" {
  description = "Map of service name to ECR repository ARN."
  value       = { for k, v in aws_ecr_repository.service : k => v.arn }
}

output "registry_id" {
  description = "The registry ID where the repositories were created."
  value       = values(aws_ecr_repository.service)[0].registry_id
}
