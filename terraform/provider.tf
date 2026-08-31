provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Note: the helm and kubernetes Terraform providers are intentionally not
# configured here. Cluster add-ons are installed via the helm CLI at apply time
# (see modules/helm-release), which avoids the plan-time dependency on the EKS
# cluster endpoint and keeps a single `terraform apply` working from a cold
# start.
