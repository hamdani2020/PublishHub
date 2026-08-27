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


# --- ArgoCD Installation ---
resource "helm_release" "argocd" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "7.3.11"
  namespace        = "argocd"
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "server.service.type"
    value = "ClusterIP"
  }

  depends_on = [module.eks]
}


# --- Argo Rollouts (progressive delivery) ---
resource "helm_release" "argo_rollouts" {
  name             = "argo-rollouts"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-rollouts"
  version          = "2.37.7"
  namespace        = "argo-rollouts"
  create_namespace = true
  wait             = true
  timeout          = 300

  depends_on = [module.eks]
}


# --- KEDA (event-driven autoscaling) ---
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = "2.16.1"
  namespace        = "keda"
  create_namespace = true
  wait             = true
  timeout          = 600

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

  depends_on = [module.eks, helm_release.argocd]
}
