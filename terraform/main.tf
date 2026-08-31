module "vpc" {
  source = "./modules/vpc"

  project            = var.project
  environment        = var.environment
  aws_region         = var.aws_region
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
  tags               = var.tags
}


module "eks" {
  source = "./modules/eks"

  project            = var.project
  environment        = var.environment
  cluster_version    = var.cluster_version
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids

  node_group_instance_types = var.node_group_instance_types
  node_group_capacity_type  = var.node_group_capacity_type
  node_group_desired_size   = var.node_group_desired_size
  node_group_min_size       = var.node_group_min_size
  node_group_max_size       = var.node_group_max_size

  tags = var.tags
}


module "sqs" {
  source = "./modules/sqs"

  project     = var.project
  environment = var.environment
  tags        = var.tags
}


module "ecr" {
  source = "./modules/ecr"

  project     = var.project
  environment = var.environment
  tags        = var.tags
}


module "iam" {
  source = "./modules/iam"

  project     = var.project
  environment = var.environment

  eks_oidc_issuer_url   = "https://${module.eks.oidc_provider_url}"
  eks_oidc_provider_arn = module.eks.oidc_provider_arn
  sqs_queue_arns        = [module.sqs.queue_arn, module.sqs.dlq_arn]
  ecr_repository_arns   = values(module.ecr.repository_arns)
  github_repository     = var.github_repository
  github_owner_id       = var.github_owner_id
  github_repo_id        = var.github_repo_id

  create_github_oidc_provider = var.create_github_oidc_provider

  tags = var.tags
}


# --- Cluster add-ons (installed via helm CLI at apply time) ---
# These use the helm-release module rather than the helm Terraform provider so
# that a single `terraform apply` works from a cold start. The helm/kubernetes
# providers would require the cluster endpoint at plan time, which does not
# exist yet on the first apply.

# --- ArgoCD Installation ---
module "argocd" {
  source = "./modules/helm-release"

  cluster_name     = module.eks.cluster_name
  cluster_endpoint = module.eks.cluster_endpoint
  aws_region       = var.aws_region
  release_name     = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  chart_version    = "7.3.11"
  namespace        = "argocd"
  timeout          = 600

  min_ready_nodes = var.node_group_min_size

  values = {
    "server.service.type" = "ClusterIP"
  }

  depends_on = [module.eks]
}


# --- Argo Rollouts (progressive delivery) ---
module "argo_rollouts" {
  source = "./modules/helm-release"

  cluster_name     = module.eks.cluster_name
  cluster_endpoint = module.eks.cluster_endpoint
  aws_region       = var.aws_region
  release_name     = "argo-rollouts"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-rollouts"
  chart_version    = "2.37.7"
  namespace        = "argo-rollouts"
  timeout          = 300

  min_ready_nodes = var.node_group_min_size

  depends_on = [module.eks]
}


# --- KEDA (event-driven autoscaling) ---
module "keda" {
  source = "./modules/helm-release"

  cluster_name     = module.eks.cluster_name
  cluster_endpoint = module.eks.cluster_endpoint
  aws_region       = var.aws_region
  release_name     = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  chart_version    = "2.16.1"
  namespace        = "keda"
  timeout          = 600

  min_ready_nodes = var.node_group_min_size

  depends_on = [module.eks]
}


module "argocd_app" {
  source = "./modules/argocd-app"

  project      = var.project
  environment  = var.environment
  cluster_name = module.eks.cluster_name
  repo_url     = "https://github.com/${var.github_repository}.git"

  ecr_registry  = "${module.ecr.registry_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
  sqs_queue_url = module.sqs.queue_url
  sqs_region    = var.aws_region
  irsa_role_arn = module.iam.worker_role_arn

  depends_on = [module.eks, module.argocd]
}
