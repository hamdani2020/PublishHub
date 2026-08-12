output "keda_role_arn" {
  description = "ARN of the IAM role for KEDA to read SQS queue attributes."
  value       = aws_iam_role.keda_sqs.arn
}

output "worker_role_arn" {
  description = "ARN of the IAM role for the worker to consume SQS messages."
  value       = aws_iam_role.worker.arn
}

output "github_actions_role_arn" {
  description = "ARN of the IAM role for GitHub Actions OIDC authentication."
  value       = aws_iam_role.github_actions.arn
}
