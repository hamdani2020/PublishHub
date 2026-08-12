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

  eks_oidc_issuer_url = "https://${module.eks.oidc_provider_url}"
  sqs_queue_arns      = [module.sqs.queue_arn, module.sqs.dlq_arn]
  ecr_repository_arns = values(module.ecr.repository_arns)
  github_repository   = var.github_repository

  create_github_oidc_provider = var.create_github_oidc_provider

  tags = var.tags
}
