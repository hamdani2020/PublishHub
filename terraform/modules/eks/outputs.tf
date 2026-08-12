output "cluster_name" {
  description = "Name of the EKS cluster"
  value       = aws_eks_cluster.this.name
}

output "cluster_arn" {
  description = "ARN of the EKS cluster"
  value       = aws_eks_cluster.this.arn
}

output "cluster_endpoint" {
  description = "Endpoint for the EKS cluster API server"
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_certificate_authority_data" {
  description = "Base64-encoded certificate authority data for the cluster"
  value       = aws_eks_cluster.this.certificate_authority[0].data
}

output "cluster_version" {
  description = "Kubernetes version of the EKS cluster"
  value       = aws_eks_cluster.this.version
}

output "cluster_security_group_id" {
  description = "Security group ID attached to the EKS cluster"
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "oidc_provider_arn" {
  description = "ARN of the OIDC provider for IRSA"
  value       = aws_iam_openid_connect_provider.cluster.arn
}

output "oidc_provider_url" {
  description = "URL of the OIDC provider (without https:// prefix)"
  value       = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
}

output "node_group_role_arn" {
  description = "ARN of the IAM role used by the node group"
  value       = aws_iam_role.node_group.arn
}

output "node_group_role_name" {
  description = "Name of the IAM role used by the node group"
  value       = aws_iam_role.node_group.name
}

output "cluster_role_arn" {
  description = "ARN of the IAM role used by the EKS cluster"
  value       = aws_iam_role.cluster.arn
}
