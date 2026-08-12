# AWS Deployment Runbook

This runbook covers the full lifecycle of deploying PublishHub to AWS using
Terraform: initial setup, plan review, apply, post-apply configuration,
application deployment, and teardown.

> **Important:** `terraform apply` and `terraform destroy` are always manual,
> interactive operations. There is no make target, CI job, or script that runs
> them automatically. This is by design (Requirement 13.8).

---

## 1. Prerequisites

| Tool | Minimum version | Install |
|------|-----------------|---------|
| AWS CLI | 2.x | `brew install awscli` or [docs](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Terraform | 1.10+ | `brew install terraform` or [docs](https://developer.hashicorp.com/terraform/install) |
| kubectl | 1.28+ | `brew install kubectl` |
| helm | 3.x | `brew install helm` |

### AWS permissions

The caller (IAM user or SSO role) needs at minimum:

- `ec2:*` (VPC, subnets, NAT, security groups)
- `eks:*` (cluster, node groups, OIDC provider)
- `sqs:*` (queues)
- `ecr:*` (repositories)
- `iam:*` (roles, policies, OIDC providers)
- `s3:*` (for the state backend bucket)
- `sts:GetCallerIdentity`

For a first deployment, `AdministratorAccess` works. For ongoing operations,
scope down to the services above.

### Verify credentials

```bash
aws sts get-caller-identity
```

If this fails, configure credentials with `aws configure` or refresh your SSO
session with `aws sso login --profile <profile>`.

---

## 2. Initial Setup

### 2.1 Create the state backend (one-time)

Terraform needs an S3 bucket for remote state storage. As of Terraform 1.10+,
state locking uses S3 natively via a `.tflock` file in the same bucket — no
DynamoDB table is required.

Create the bucket manually or with a minimal bootstrap configuration:

```bash
# Create the S3 bucket for state
aws s3api create-bucket \
  --bucket publishhub-tfstate-ACCOUNT_ID \
  --region us-east-1

# Enable versioning (protects against accidental state corruption)
aws s3api put-bucket-versioning \
  --bucket publishhub-tfstate-ACCOUNT_ID \
  --versioning-configuration Status=Enabled

```

Replace `ACCOUNT_ID` with your AWS account ID to ensure uniqueness.

### 2.2 Configure the backend

```bash
cd terraform/
cp backend.tf.example backend.tf
```

Edit `backend.tf` with your actual bucket name:

```hcl
terraform {
  backend "s3" {
    bucket       = "publishhub-tfstate-123456789012"
    key          = "publishhub/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}
```

> **Note:** `use_lockfile = true` enables S3-native state locking (Terraform
> 1.10+). The older `dynamodb_table` argument is deprecated and will be removed
> in a future release.

> Do NOT commit `backend.tf` with real values. The `.gitignore` already excludes
> it from version control.

### 2.3 Create a variables file

```bash
cat > terraform.tfvars <<'EOF'
aws_region          = "us-east-1"
environment         = "production"
github_repository   = "your-org/publishhub"
EOF
```

> Do NOT commit `terraform.tfvars`. It is already gitignored.

### 2.4 Initialize Terraform

```bash
terraform init
```

This downloads providers, configures the backend, and prepares the working
directory. Run it again any time you change the backend or add modules.

---

## 3. Plan Review

Always review a plan before applying:

```bash
terraform plan -out=tfplan
```

The plan shows every resource that will be created, modified, or destroyed.
Review it carefully:

- Verify the VPC CIDR does not conflict with existing infrastructure.
- Confirm the EKS cluster version matches your workload requirements.
- Check that instance types (`t4g.medium`, `t4g.large`) are available in your
  region.
- Confirm Spot capacity is acceptable for your workload (it is the default for
  cost optimization).

### What gets created

| Module | Resources |
|--------|-----------|
| `vpc` | VPC, 3 public subnets, 3 private subnets, Internet Gateway, 1 NAT Gateway, route tables |
| `eks` | EKS cluster, managed node group (Spot, ARM), OIDC provider, IAM cluster role |
| `sqs` | Main queue, dead-letter queue with redrive policy |
| `ecr` | Repositories for api, worker, web with lifecycle policies |
| `iam` | IRSA roles (KEDA, worker), GitHub Actions OIDC role |

### Cost implications

See [Section 7: Daily Cost Estimate](#7-daily-cost-estimate) for a breakdown.
Understand the costs before applying.

---

## 4. Apply

> **This is a manual, interactive operation.** Terraform will show the plan and
> prompt you to type `yes` to proceed. There is no automated path to apply.

```bash
terraform apply tfplan
```

Or without a saved plan (Terraform will re-plan and prompt):

```bash
terraform apply
```

Terraform will display the plan and ask:

```
Do you want to perform these actions?
  Terraform will perform the actions described above.
  Only 'yes' will be accepted to approve.

  Enter a value:
```

Type `yes` to proceed.

### Timing

- EKS cluster creation takes **15-20 minutes**. This is normal.
- The full apply typically completes in **20-25 minutes**.
- Do not interrupt the process. If you must cancel, use Ctrl+C once and wait for
  Terraform to roll back gracefully.

### Outputs

On success, Terraform prints outputs:

```
cluster_name            = "publishhub-production"
aws_region              = "us-east-1"
ecr_repository_urls     = { api = "123456789012.dkr.ecr...", worker = "...", web = "..." }
sqs_queue_url           = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-production"
sqs_dlq_url             = "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-production-dlq"
cluster_endpoint        = "https://XXXXX.gr7.us-east-1.eks.amazonaws.com"
github_actions_role_arn = "arn:aws:iam::123456789012:role/publishhub-production-github-actions"
```

Save these. You need them for the next steps.

---

## 5. Post-Apply Configuration

### 5.1 Configure kubectl

```bash
aws eks update-kubeconfig \
  --name $(terraform output -raw cluster_name) \
  --region $(terraform output -raw aws_region)
```

### 5.2 Verify cluster connectivity

```bash
kubectl cluster-info
kubectl get nodes
```

You should see nodes in `Ready` state with ARM64 architecture (Graviton).

### 5.3 Update Helm production values

Edit `helm/publishhub/values-production.yaml` with the Terraform outputs:

```yaml
global:
  image:
    registry: "123456789012.dkr.ecr.us-east-1.amazonaws.com"

queue:
  backend: sqs
  sqs:
    queueUrl: "https://sqs.us-east-1.amazonaws.com/123456789012/publishhub-production"

api:
  image:
    repository: "123456789012.dkr.ecr.us-east-1.amazonaws.com/publishhub-api"

worker:
  image:
    repository: "123456789012.dkr.ecr.us-east-1.amazonaws.com/publishhub-worker"

web:
  image:
    repository: "123456789012.dkr.ecr.us-east-1.amazonaws.com/publishhub-web"
```

Replace the account ID and region with your actual Terraform output values.

### 5.4 Push images to ECR

Authenticate Docker to ECR:

```bash
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com
```

Build and push:

```bash
for app in api worker web; do
  docker build -t "123456789012.dkr.ecr.us-east-1.amazonaws.com/publishhub-${app}:latest" "apps/${app}"
  docker push "123456789012.dkr.ecr.us-east-1.amazonaws.com/publishhub-${app}:latest"
done
```

---

## 6. Deploying the Application

### 6.1 Install the platform layer

```bash
make platform-install
```

This installs ArgoCD, KEDA, and Argo Rollouts on the EKS cluster.

### 6.2 Deploy with ArgoCD

Ensure the ArgoCD Application references your production values file, then sync:

```bash
make argocd-sync
```

### 6.3 Verify deployment

```bash
kubectl get pods -n publishhub
kubectl get scaledobject -n publishhub
kubectl get applications -n argocd
```

All pods should be running and the ArgoCD Application should show `Healthy` and
`Synced`.

---

## 7. Daily Cost Estimate

The following is a realistic estimate for the default configuration running in
`us-east-1`. Actual costs vary by region, usage, and Spot pricing fluctuations.

| Resource | Estimated daily cost | Notes |
|----------|---------------------|-------|
| EKS control plane | ~$2.40/day | Fixed ($0.10/hr) |
| NAT Gateway | ~$1.08/day | $0.045/hr + $0.045/GB data processed |
| EC2 instances (2x t4g.medium, Spot) | ~$1.00-2.00/day | Spot pricing varies; ~60-70% savings vs On-Demand |
| SQS | negligible | Free tier covers 1M requests/month |
| ECR | negligible | Storage only; lifecycle policy limits retained images |
| S3 (state bucket) | negligible | Single small file |

### Total estimated cost: ~$5-7/day (~$150-210/month)

> **The single most effective cost-control action is to run `terraform destroy`
> when the environment is not in use.** See Section 8.

---

## 8. Teardown

> ---
>
> **WARNING: CRITICAL COST-CONTROL STEP**
>
> ---
>
> **`terraform destroy` removes ALL billable AWS resources created by this
> configuration.** This is the explicit cost-control mechanism for the
> PublishHub AWS deployment. If you do not destroy the infrastructure when it
> is not in use, you will be charged approximately $5-7/day continuously.
>
> ---

### Running destroy

```bash
cd terraform/
terraform destroy
```

Terraform will display every resource that will be destroyed and prompt:

```
Do you really want to destroy all resources?
  Terraform will destroy all your managed infrastructure, as shown above.
  There is no undo. Only 'yes' will be accepted to confirm.

  Enter a value:
```

Type `yes` to confirm.

> **This is a manual, interactive operation.** There is no make target, CI job,
> or script that runs `terraform destroy`. This is intentional. Destroying
> billable infrastructure requires explicit human confirmation every time
> (Requirement 13.8).

### What gets removed

All resources created by `terraform apply` are removed:

- EKS cluster and node groups
- VPC, subnets, NAT Gateway, Internet Gateway
- SQS queues (main and DLQ)
- ECR repositories (and all images in them)
- IAM roles and policies
- Security groups and route tables

After destroy, the only remaining AWS resource is the state backend S3 bucket
which you created manually and must remove separately if you want to fully
clean up:

```bash
# Optional: remove state backend (only after terraform destroy)
aws s3 rb s3://publishhub-tfstate-ACCOUNT_ID --force
```

### Timing

Destroy typically takes **10-15 minutes**, primarily waiting for the EKS cluster
and NAT Gateway to be fully removed.

---

## 9. Troubleshooting

### Permission errors during plan or apply

```
Error: error creating EKS Cluster: AccessDeniedException
```

Your IAM principal lacks the required permissions. Check that your caller
identity (`aws sts get-caller-identity`) has the permissions listed in
Section 1.

### Region or AZ capacity

```
Error: error creating EKS Node Group: InvalidParameterException: 
  Instances of type t4g.medium are not available in us-east-1e
```

Not all instance types are available in every AZ. Either reduce `az_count` or
adjust `node_group_instance_types` in your `terraform.tfvars`:

```hcl
node_group_instance_types = ["t4g.medium", "t4g.large", "m7g.medium"]
az_count                  = 2
```

### EKS cluster unreachable after apply

```
Unable to connect to the server: dial tcp: lookup XXXXX.gr7.us-east-1.eks.amazonaws.com: no such host
```

Wait 1-2 minutes after apply. The cluster endpoint DNS can take time to
propagate. Then re-run:

```bash
aws eks update-kubeconfig --name $(terraform output -raw cluster_name) --region $(terraform output -raw aws_region)
kubectl cluster-info
```

### Spot capacity unavailable

```
Error: Insufficient capacity (InsufficientInstanceCapacity)
```

Spot interruptions or capacity shortages. Options:

1. Wait and retry — Spot capacity fluctuates.
2. Switch to On-Demand temporarily:
   ```hcl
   node_group_capacity_type = "ON_DEMAND"
   ```
3. Add more instance type diversity to the list.

### State lock contention

```
Error: Error acquiring the state lock
```

Another process holds the lock. If you are certain no other apply is running:

```bash
terraform force-unlock <LOCK_ID>
```

Use with caution. This should only be needed after a crash or timeout.

### ECR login failures

```
Error: Cannot perform an interactive login from a non TTY device
```

Ensure you pipe the password correctly:

```bash
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
  $(terraform output -raw ecr_repository_urls | jq -r 'to_entries[0].value' | cut -d/ -f1)
```

### GitHub OIDC provider already exists

```
Error: creating IAM OIDC Provider: EntityAlreadyExists
```

If another project already created the GitHub OIDC provider in this account:

```hcl
create_github_oidc_provider = false
```

---

## Quick Reference

| Action | Command | Interactive? |
|--------|---------|--------------|
| Initialize | `terraform init` | No |
| Format check | `terraform fmt -check -recursive` | No |
| Validate | `terraform validate` | No |
| Plan | `terraform plan` | No |
| **Apply** | **`terraform apply`** | **Yes — type "yes"** |
| **Destroy** | **`terraform destroy`** | **Yes — type "yes"** |
| Show outputs | `terraform output` | No |

> Apply and destroy are the only operations that modify AWS resources. They
> always require interactive confirmation. This project deliberately provides
> no automation for these operations (Requirement 13.8).
