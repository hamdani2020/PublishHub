# PublishHub — Complete Execution Guide

> **Goal:** Build and run a production-like developer platform that mirrors Buffer's infrastructure stack, using Amazon Bedrock for AI-assisted incident response.
>
> **Time Estimate:** 4–6 hours for initial setup, 2–3 hours for AWS deployment.
> **Cost:** ~$0 for local development; ~$5–15/day for AWS EKS (remember to destroy when done).

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Project Overview](#2-project-overview)
3. [Phase 1: Local Environment Setup](#3-phase-1-local-environment-setup)
4. [Phase 2: Build the Applications](#4-phase-2-build-the-applications)
5. [Phase 3: Install Platform Components](#5-phase-3-install-platform-components)
6. [Phase 4: Deploy with ArgoCD](#6-phase-4-deploy-with-argocd)
7. [Phase 5: Test KEDA Autoscaling](#7-phase-5-test-keda-autoscaling)
8. [Phase 6: Argo Rollouts (Canary Deployments)](#8-phase-6-argo-rollouts-canary-deployments)
9. [Phase 7: Developer CLI](#9-phase-7-developer-cli)
10. [Phase 8: AI Incident Analyzer with Amazon Bedrock](#10-phase-8-ai-incident-analyzer-with-amazon-bedrock)
11. [Phase 9: Terraform AWS Infrastructure](#11-phase-9-terraform-aws-infrastructure)
12. [Phase 10: Observability with Datadog](#12-phase-10-observability-with-datadog)
13. [Phase 11: CI/CD with GitHub Actions](#13-phase-11-cicd-with-github-actions)
14. [Troubleshooting](#14-troubleshooting)
15. [Next Steps & Portfolio](#15-next-steps--portfolio)

---

## 1. Prerequisites

### Required Tools

| Tool | Purpose | Install Command (macOS) |
|------|---------|------------------------|
| Docker | Container runtime | `brew install --cask docker` or OrbStack |
| OrbStack | Lightweight Docker/K8s for Mac (recommended) | `brew install --cask orbstack` |
| kind | Local Kubernetes cluster | `brew install kind` |
| kubectl | K8s command-line tool | `brew install kubectl` |
| Helm | K8s package manager | `brew install helm` |
| Terraform | Infrastructure as Code | `brew install terraform` |
| Node.js 20+ | API and Web apps | `brew install node` |
| Python 3.11+ | Worker and CLI | `brew install python` |
| AWS CLI | AWS authentication | `brew install awscli` |
| yq | YAML processor (for CI) | `brew install yq` |

### Verify Installation

```bash
docker --version          # Should show 24.x or higher
kind version              # Should show 0.20+
kubectl version           # Should show 1.28+
helm version              # Should show 3.13+
terraform --version       # Should show 1.5+
node --version            # Should show v20+
python3 --version         # Should show 3.11+
aws --version             # Should show 2.x
```

### AWS Setup (for later phases)

```bash
# Configure AWS CLI with your credentials
aws configure
# Enter: AWS Access Key ID, Secret Access Key, default region (us-east-1), output format (json)

# Verify
aws sts get-caller-identity
```

### Amazon Bedrock Setup (for AI Incident Analyzer)

1. Go to AWS Console → Amazon Bedrock
2. Click "Model access" in the left sidebar
3. Request access to **Claude 3 Haiku** (fastest, cheapest) and/or **Claude 3 Sonnet**
4. Wait for approval (usually instant)
5. Note your AWS region (e.g., `us-east-1`)

---

## 2. Project Overview

### Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  React Web  |────>|  Node API   |────>| SQS Queue   |
│  (Frontend) |     |  (Gateway)  |     |  (Messages) |
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               |
                                               v
                                        ┌─────────────┐
                                        │ KEDA Scaler │
                                        │ (Watches    │
                                        │  queue)     │
                                        └──────┬──────┘
                                               |
                                               v
                                        ┌─────────────┐
                                        │ Python      │
                                        │ Worker      │
                                        │ (Processes  │
                                        │  posts)     │
                                        └─────────────┘
```

**Platform Layer (ArgoCD manages all of this):**
- ArgoCD -> GitOps (automatic deployments from Git)
- Argo Rollouts -> Canary deployments with auto-rollback
- KEDA -> Scale workers based on queue depth
- Datadog -> Metrics, traces, logs
- Terraform -> AWS infrastructure (EKS, SQS, ECR)

### Directory Structure

```
publishhub/
├── apps/
│   ├── api/              # Node.js API gateway
│   ├── worker/           # Python background worker
│   └── web/              # React frontend
├── helm/
│   └── publishhub/       # Helm chart for K8s deployment
├── argocd/
│   ├── bootstrap.yaml    # App of Apps bootstrap
│   ├── applications/     # ArgoCD Application manifests
│   ├── projects/         # ArgoCD Project (RBAC)
│   └── rollouts/         # Argo Rollout (canary config)
├── terraform/
│   ├── main.tf           # Root Terraform config
│   ├── variables.tf      # Input variables
│   └── modules/
│       ├── vpc/          # VPC module
│       └── eks/          # EKS module
├── cli/
│   └── publishctl/       # Developer CLI tool
├── scripts/
│   ├── kind-with-registry.sh
│   └── ai-incident-analyzer.py   # Bedrock-powered AI analyzer
├── observability/
│   └── datadog/          # Monitors, dashboards
├── docs/
│   ├── architecture.md
│   ├── local-dev.md
│   └── runbooks/
├── .github/
│   └── workflows/        # GitHub Actions CI/CD
├── Makefile
└── README.md
```

---

## 3. Phase 1: Local Environment Setup

### Step 1.1: Create the Project Directory

```bash
# Create project root
mkdir -p ~/projects/publishhub
cd ~/projects/publishhub

# Initialize Git repo
git init
git checkout -b main
```

### Step 1.2: Create the Makefile

Create `~/projects/publishhub/Makefile` with the content from the project files.

Key commands:
- `make cluster-up` - Create local K8s cluster
- `make platform-install` - Install ArgoCD, KEDA, Argo Rollouts
- `make apps-build` - Build all Docker images
- `make argocd-sync` - Deploy via ArgoCD
- `make clean` - Tear everything down

### Step 1.3: Create the Kind Cluster Script

Create `~/projects/publishhub/scripts/kind-with-registry.sh` and make it executable:

```bash
chmod +x scripts/kind-with-registry.sh
```

### Step 1.4: Create the Cluster

```bash
make cluster-up
```

**Expected output:**
```
Cluster publishhub-cluster is ready with registry at localhost:5001
```

**Verify:**
```bash
kubectl get nodes
# Should show: publishhub-cluster-control-plane   Ready
```

---

## 4. Phase 2: Build the Applications

### Step 2.1: Create the API (Node.js)

```bash
mkdir -p apps/api/src
```

Create these files:
- `apps/api/package.json`
- `apps/api/tsconfig.json`
- `apps/api/src/index.ts`
- `apps/api/Dockerfile`

**Build the API:**

```bash
cd apps/api
npm install
npm run build
cd ../..
```

### Step 2.2: Create the Worker (Python)

```bash
mkdir -p apps/worker
```

Create these files:
- `apps/worker/requirements.txt`
- `apps/worker/main.py`
- `apps/worker/Dockerfile`

### Step 2.3: Create the Web Frontend (React)

```bash
mkdir -p apps/web/src
```

Create these files:
- `apps/web/package.json`
- `apps/web/index.html`
- `apps/web/src/main.tsx`
- `apps/web/src/App.tsx`
- `apps/web/src/App.css`
- `apps/web/Dockerfile`
- `apps/web/nginx.conf`

### Step 2.4: Build All Images

```bash
make apps-build
```

**Verify images are in local registry:**
```bash
curl http://localhost:5001/v2/_catalog
# Should show: {"repositories":["publishhub-api","publishhub-worker","publishhub-web"]}
```

---

## 5. Phase 3: Install Platform Components

### Step 3.1: Install ArgoCD, KEDA, and Argo Rollouts

```bash
make platform-install
```

This will:
1. Create the `argocd` namespace and install ArgoCD
2. Add the KEDA Helm repo and install KEDA into the `keda` namespace
3. Install Argo Rollouts into the `argo-rollouts` namespace
4. Wait for ArgoCD server to be ready

**Verify installations:**

```bash
# ArgoCD
kubectl get pods -n argocd
# Should show: argocd-server, argocd-repo-server, argocd-application-controller all Running

# KEDA
kubectl get pods -n keda
# Should show: keda-operator, keda-operator-metrics-apiserver Running

# Argo Rollouts
kubectl get pods -n argo-rollouts
# Should show: argo-rollouts Running
```

### Step 3.2: Install Argo Rollouts kubectl plugin

```bash
# macOS
curl -LO https://github.com/argoproj/argo-rollouts/releases/latest/download/kubectl-argo-rollouts-darwin-amd64
chmod +x ./kubectl-argo-rollouts-darwin-amd64
sudo mv ./kubectl-argo-rollouts-darwin-amd64 /usr/local/bin/kubectl-argo-rollouts

# Verify
kubectl argo rollouts version
```

---

## 6. Phase 4: Deploy with ArgoCD

### Step 4.1: Create ArgoCD Project

Create `argocd/projects/publishhub.yaml` with the project manifest.

### Step 4.2: Create the Bootstrap Application

Create `argocd/bootstrap.yaml`:

> **Important:** Replace `YOUR_GITHUB_USERNAME` with your actual GitHub username. For local testing, you can use a local path or set up a Git repo.

### Step 4.3: Create the Main Application

Create `argocd/applications/publishhub.yaml`.

### Step 4.4: Create the Helm Chart

Create the Helm chart structure:

```bash
mkdir -p helm/publishhub/templates
```

Create these files:
- `helm/publishhub/Chart.yaml`
- `helm/publishhub/values.yaml`
- `helm/publishhub/templates/_helpers.tpl`
- `helm/publishhub/templates/api-deployment.yaml`
- `helm/publishhub/templates/api-service.yaml`
- `helm/publishhub/templates/api-hpa.yaml`
- `helm/publishhub/templates/worker-deployment.yaml`
- `helm/publishhub/templates/worker-service.yaml`
- `helm/publishhub/templates/worker-keda.yaml`
- `helm/publishhub/templates/web-deployment.yaml`
- `helm/publishhub/templates/web-service.yaml`
- `helm/publishhub/templates/redis.yaml`

### Step 4.5: Deploy Everything

```bash
make argocd-sync
```

**Verify:**

```bash
# Check ArgoCD applications
kubectl get applications -n argocd
# Should show: publishhub-bootstrap, publishhub

# Check all pods in publishhub namespace
kubectl get pods -n publishhub
# Should show: api, worker, web, redis all Running

# Check KEDA scaled object
kubectl get scaledobject -n publishhub
# Should show: publishhub-worker
```

### Step 4.6: Access the Services

```bash
# Terminal 1: ArgoCD UI
make argocd-port-forward
# Open: https://localhost:8080
# Username: admin
# Password: make argocd-password

# Terminal 2: Web App
make web-port-forward
# Open: http://localhost:3000

# Terminal 3: API
make api-port-forward
# Test: curl http://localhost:8081/health
```

---

## 7. Phase 5: Test KEDA Autoscaling

### Step 5.1: Verify KEDA is Watching the Queue

```bash
# Check KEDA metrics
kubectl get hpa -n publishhub
# Should show: keda-hpa-publishhub-worker

# Describe the scaled object
kubectl describe scaledobject publishhub-worker -n publishhub
```

### Step 5.2: Generate Load and Watch Scaling

```bash
# Terminal 1: Watch pods
kubectl get pods -n publishhub -w

# Terminal 2: Send many posts to create queue depth
for i in {1..50}; do
  curl -X POST http://localhost:8081/api/v1/publish \
    -H "Content-Type: application/json" \
    -d "{\"content\": \"Test post $i\", \"platforms\": [\"twitter\", \"linkedin\"]}"
done
```

**Expected behavior:**
1. Posts are enqueued to Redis
2. KEDA detects queue depth > 10
3. Worker pods scale from 0 -> 1 -> 2 -> more
4. After queue is drained, workers scale back down

### Step 5.3: Verify in KEDA Logs

```bash
kubectl logs -n keda deployment/keda-operator | grep publishhub
```

---

## 8. Phase 6: Argo Rollouts (Canary Deployments)

### Step 6.1: Create the Rollout Manifest

Create `argocd/rollouts/api-rollout.yaml` with the canary deployment configuration.

### Step 6.2: Apply the Rollout

```bash
kubectl apply -f argocd/rollouts/api-rollout.yaml
```

### Step 6.3: Trigger a Canary Deployment

```bash
# Build a new version
docker build -t localhost:5001/publishhub-api:v2.0.0 -f apps/api/Dockerfile apps/api
docker push localhost:5001/publishhub-api:v2.0.0

# Update the rollout image
kubectl argo rollouts set image publishhub-api api=localhost:5001/publishhub-api:v2.0.0 -n publishhub

# Watch the rollout progress
kubectl argo rollouts get rollout publishhub-api -n publishhub --watch
```

**Expected output:**
```
Name:            publishhub-api
Namespace:       publishhub
Status:          Paused
Strategy:        Canary
  Step:          1/6
  SetWeight:     10
  ActualWeight:  10
Images:          localhost:5001/publishhub-api:latest (stable)
                 localhost:5001/publishhub-api:v2.0.0 (canary)
Replicas:
  Desired:       3
  Current:       4
  Updated:       1
  Ready:         4
  Available:     4
```

### Step 6.4: Promote or Abort

```bash
# If everything looks good, promote to next step
kubectl argo rollouts promote publishhub-api -n publishhub

# If something is wrong, abort immediately
kubectl argo rollouts abort publishhub-api -n publishhub
```

---

## 9. Phase 7: Developer CLI

### Step 7.1: Create the CLI Structure

```bash
mkdir -p cli/publishctl/publishctl
```

Create:
- `cli/publishctl/setup.py`
- `cli/publishctl/publishctl/cli.py`
- `cli/publishctl/publishctl/__init__.py` (empty)

### Step 7.2: Install the CLI

```bash
cd cli/publishctl
pip install -e .
cd ../..

# Test
publishctl --help
publishctl env start    # Start everything
publishctl logs --service api --tail
```

---

## 10. Phase 8: AI Incident Analyzer with Amazon Bedrock

### Step 10.1: Why Amazon Bedrock?

Instead of calling OpenAI or Anthropic directly (which requires separate API keys and billing), **Amazon Bedrock** lets you:
- Use Claude, Llama, and other models through your **existing AWS account**
- Stay within your AWS security perimeter (IAM roles, VPC endpoints)
- Pay only for what you use through AWS billing
- Use **Claude 3 Haiku** (fast, cheap) for incident analysis

### Step 10.2: IAM Permissions

Your AWS user or role needs these permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
    }
  ]
}
```

### Step 10.3: Create the Bedrock Incident Analyzer

Create `scripts/ai-incident-analyzer.py`:

```python
#!/usr/bin/env python3
"""
AI Incident Analyzer for PublishHub
Uses Amazon Bedrock (Claude 3 Haiku) for analysis

Usage:
    python scripts/ai-incident-analyzer.py --pod publishhub-api-xxx
    
Prerequisites:
    - AWS CLI configured with Bedrock access
    - Model access granted in AWS Bedrock console
"""

import argparse
import json
import subprocess
import boto3
from botocore.exceptions import ClientError


def get_pod_data(pod_name: str, namespace: str = 'publishhub') -> dict:
    """Collect Kubernetes diagnostic data for a pod."""
    print(f"Gathering data for pod: {pod_name}...")
    
    desc_result = subprocess.run(
        ["kubectl", "describe", "pod", pod_name, "-n", namespace],
        capture_output=True, text=True
    )
    description = desc_result.stdout if desc_result.returncode == 0 else f'Error: {desc_result.stderr}'
    
    logs_result = subprocess.run(
        ["kubectl", "logs", pod_name, "-n", namespace, "--tail=200"],
        capture_output=True, text=True
    )
    logs = logs_result.stdout if logs_result.returncode == 0 else f'Error: {logs_result.stderr}'
    
    prev_logs_result = subprocess.run(
        ["kubectl", "logs", pod_name, "-n", namespace, "--tail=100", "--previous"],
        capture_output=True, text=True
    )
    prev_logs = prev_logs_result.stdout if prev_logs_result.returncode == 0 else ''
    
    events_result = subprocess.run(
        ["kubectl", "get", "events", "-n", namespace,
         "--field-selector", f"involvedObject.name={pod_name}",
         "--sort-by=.lastTimestamp"],
        capture_output=True, text=True
    )
    events = events_result.stdout if events_result.returncode == 0 else f'Error: {events_result.stderr}'
    
    return {
        "pod_name": pod_name,
        "namespace": namespace,
        "description": description,
        "logs": logs,
        "previous_logs": prev_logs,
        "events": events,
    }


def analyze_with_bedrock(data: dict, region: str = 'us-east-1') -> str:
    """Send incident data to Amazon Bedrock (Claude 3 Haiku) for analysis."""
    
    bedrock = boto3.client("bedrock-runtime", region_name=region)
    model_id = "anthropic.claude-3-haiku-20240307-v1:0"
    
    prompt = f"""You are an expert Site Reliability Engineer analyzing a Kubernetes pod incident.
Provide a concise, structured analysis in this exact format:

## SUMMARY
One sentence describing what happened.

## ROOT CAUSE (Top 3 Hypotheses)
- Hypothesis 1: ...
- Hypothesis 2: ...
- Hypothesis 3: ...

## RECOMMENDED FIX
Specific commands or configuration changes to resolve the issue.

## SEVERITY
Low / Medium / High -- with justification.

## CATEGORY
Code bug / Configuration issue / Infrastructure issue / Resource exhaustion

---

POD DESCRIPTION:
{data['description'][:3000]}

RECENT LOGS:
{data['logs'][:4000]}

PREVIOUS CONTAINER LOGS (if pod restarted):
{data['previous_logs'][:2000] if data['previous_logs'] else 'No previous logs available'}

EVENTS:
{data['events'][:2000]}
"""
    
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ]
    }
    
    try:
        response = bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps(body)
        )
        
        response_body = json.loads(response['body'].read())
        return response_body['content'][0]['text']
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        return f'Error calling Bedrock: {error_code} - {error_message}'
    except Exception as e:
        return f'Unexpected error: {str(e)}'


def main():
    parser = argparse.ArgumentParser(description="AI Incident Analyzer using Amazon Bedrock")
    parser.add_argument("--pod", required=True, help="Name of the pod to analyze")
    parser.add_argument("--namespace", default="publishhub", help="Kubernetes namespace")
    parser.add_argument("--region", default="us-east-1", help="AWS region for Bedrock")
    parser.add_argument("--output", choices=["text", "json"], default="text", help="Output format")
    
    args = parser.parse_args()
    
    data = get_pod_data(args.pod, args.namespace)
    
    print("\nSending to Amazon Bedrock (Claude 3 Haiku) for analysis...")
    analysis = analyze_with_bedrock(data, args.region)
    
    if args.output == "json":
        output = {
            "pod": args.pod,
            "namespace": args.namespace,
            "analysis": analysis,
            "raw_data": data
        }
        print(json.dumps(output, indent=2))
    else:
        print("\n" + "=" * 70)
        print("INCIDENT ANALYSIS REPORT")
        print("=" * 70)
        print(f"Pod: {args.pod}")
        print(f"Namespace: {args.namespace}")
        print("Model: Claude 3 Haiku via Amazon Bedrock")
        print("=" * 70)
        print(analysis)
        print("=" * 70)


if __name__ == "__main__":
    main()
```

Make it executable:

```bash
chmod +x scripts/ai-incident-analyzer.py
```

### Step 10.4: Test the Analyzer

First, create a broken pod to analyze:

```bash
# Create a pod that will crash
kubectl run broken-pod --image=busybox --restart=Never -n publishhub -- /bin/false

# Wait a few seconds for it to fail
sleep 5

# Now analyze it
python scripts/ai-incident-analyzer.py --pod broken-pod --namespace publishhub
```

### Step 10.5: Integrate with the CLI

Update `cli/publishctl/publishctl/cli.py` to add the incident command:

```python
@main.command()
@click.option("--pod", required=True, help="Pod name to analyze")
@click.option("--namespace", default="publishhub", help="Kubernetes namespace")
def incident(pod, namespace):
    """AI-assisted incident analysis using Amazon Bedrock"""
    console.print(f"[bold]Analyzing pod {pod}...[/]")
    subprocess.run([
        "python", "scripts/ai-incident-analyzer.py",
        "--pod", pod,
        "--namespace", namespace
    ], check=True)
```

Reinstall the CLI:

```bash
cd cli/publishctl
pip install -e .
cd ../..

# Use it
publishctl incident --pod publishhub-api-xxx
```

---
## 11. Phase 9: Terraform AWS Infrastructure

### Step 9.1: Create Terraform Structure

```bash
mkdir -p terraform/modules/eks terraform/modules/vpc
```

Create these files:
- `terraform/main.tf`
- `terraform/variables.tf`
- `terraform/modules/vpc/main.tf`
- `terraform/modules/eks/main.tf`

### Step 9.2: Initialize and Plan

```bash
cd terraform
terraform init
terraform plan -out=tfplan
```

### Step 9.3: Apply Infrastructure

```bash
terraform apply tfplan
```

This creates:
- VPC with public/private subnets across 3 AZs
- EKS cluster with managed node groups
- SQS queue with dead-letter queue
- ECR repositories for each service
- IAM roles with IRSA for KEDA

### Step 9.4: Configure kubectl

```bash
aws eks update-kubeconfig --region us-east-1 --name publishhub-production
kubectl get nodes
```

### Step 9.5: Destroy When Done

```bash
terraform destroy
```

---

## 12. Phase 10: Observability with Datadog

### Step 10.1: Sign Up for Datadog

1. Go to https://www.datadoghq.com and sign up for a free 14-day trial
2. Get your API key from Organization Settings -> API Keys
3. Get your APP key from Organization Settings -> Application Keys

### Step 10.2: Install Datadog Agent in Cluster

```bash
helm repo add datadog https://helm.datadoghq.com
helm repo update

helm install datadog-agent datadog/datadog \
  --namespace datadog --create-namespace \
  --set datadog.apiKey=YOUR_API_KEY \
  --set datadog.appKey=YOUR_APP_KEY \
  --set datadog.site=datadoghq.com \
  --set datadog.tags={env:production,project:publishhub} \
  --set datadog.apm.enabled=true \
  --set datadog.logs.enabled=true \
  --set datadog.processAgent.enabled=true
```

### Step 10.3: Verify APM Traces

```bash
# Send a test request
curl -X POST http://localhost:8081/api/v1/publish -H 'Content-Type: application/json' -d '{"content": "Test", "platforms": ["twitter"]}'

# Check Datadog APM -> Traces in the UI
```

### Step 10.4: Create Monitors

Create `observability/datadog/monitors.yaml` and import into Datadog UI or via Terraform.

---

## 13. Phase 11: CI/CD with GitHub Actions

### Step 11.1: Create GitHub Repository

```bash
gh repo create publishhub --public --source=. --remote=origin
git add .
git commit -m "Initial commit: PublishHub platform"
git push -u origin main
```

### Step 11.2: Create GitHub Actions Workflow

Create `.github/workflows/deploy.yaml`:

```yaml
name: Build and Deploy

on:
  push:
    branches: [main]
    tags: ['v*']
  pull_request:
    branches: [main]

env:
  AWS_REGION: us-east-1
  ECR_REGISTRY: ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    strategy:
      matrix:
        service: [api, worker, web]
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS Credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to Amazon ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: ./apps/${{ matrix.service }}
          push: ${{ github.event_name != 'pull_request' }}
          tags: |
            ${{ env.ECR_REGISTRY }}/publishhub-production/${{ matrix.service }}:${{ github.sha }}
            ${{ env.ECR_REGISTRY }}/publishhub-production/${{ matrix.service }}:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Update image tag in Helm values
        if: github.event_name != 'pull_request'
        run: |
          yq e ".${{ matrix.service }}.image.tag = \"${{ github.sha }}\"" -i helm/publishhub/values.yaml

      - name: Commit updated values
        if: github.event_name != 'pull_request'
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@github.com"
          git add helm/publishhub/values.yaml
          git diff --cached --quiet || git commit -m "ci: update ${{ matrix.service }} image to ${{ github.sha }}"
          git push
```

### Step 11.3: Add GitHub Secrets

In GitHub repo Settings -> Secrets and variables -> Actions:

| Secret Name | Value |
|-------------|-------|
| `AWS_ACCOUNT_ID` | Your AWS account ID (12 digits) |
| `AWS_ROLE_ARN` | IAM role ARN for GitHub Actions OIDC |

---

## 14. Troubleshooting

### Issue: Images fail to push to local registry

```bash
# Check registry is running
docker ps | grep publishhub-registry

# Check cluster can reach registry
docker network inspect kind
```

### Issue: ArgoCD shows Sync Failed

```bash
# Check ArgoCD logs
kubectl logs -n argocd deployment/argocd-application-controller | tail -50

# Check if repo URL is correct
kubectl get applications -n argocd -o yaml | grep repoURL
```

### Issue: KEDA not scaling workers

```bash
# Check KEDA operator logs
kubectl logs -n keda deployment/keda-operator

# Check scaled object status
kubectl describe scaledobject publishhub-worker -n publishhub

# Check if Redis is accessible
kubectl exec -it deployment/publishhub-redis-master -n publishhub -- redis-cli ping
```

### Issue: Bedrock access denied

```bash
# Verify AWS credentials
aws sts get-caller-identity

# Check Bedrock model access in AWS Console
# Go to: Amazon Bedrock -> Model access
# Ensure 'Claude 3 Haiku' shows as 'Access granted'
```

### Issue: Worker pods crash immediately

```bash
# Check worker logs
kubectl logs -n publishhub -l app=worker --tail=50

# Check if Redis URL is correct in environment
kubectl get deployment publishhub-worker -n publishhub -o yaml | grep REDIS_URL
```

---

## 15. Next Steps & Portfolio

### What to Put on Your Resume

Add this project to your resume with a GitHub link:

> **PublishHub** — Production-like developer platform demo
> [github.com/YOUR_USERNAME/publishhub]
> 
> - GitOps with ArgoCD (App of Apps pattern, automated sync, RBAC)
> - Progressive delivery with Argo Rollouts (canary deployments, auto-rollback)
> - Event-driven autoscaling with KEDA (SQS queue depth, scale-from-zero)
> - AI-assisted incident response using Amazon Bedrock (Claude 3 Haiku)
> - Infrastructure as Code with Terraform (EKS, VPC, Spot instances, IRSA)
> - Observability with Datadog APM, custom metrics, and structured logging
> - Developer experience tooling (publishctl CLI, per-PR environments)
> - CI/CD with GitHub Actions (OIDC, multi-service builds, automated Helm updates)

### Demo Script for Interviews

Prepare a 3-minute screen recording showing:
1. **ArgoCD UI** (localhost:8080) — show synced applications, click into publishhub app
2. **Web UI** (localhost:3000) — type a post, click Publish, show 'Queued!' response
3. **KEDA scaling** — `kubectl get pods -w` while sending 50 posts, show workers scaling up
4. **Argo Rollout** — `kubectl argo rollouts get rollout publishhub-api -n publishhub`
5. **AI Incident Analyzer** — run `publishctl incident --pod broken-pod`, show Bedrock analysis
6. **Datadog dashboard** — show APM traces and custom metrics

### Key Talking Points for Buffer Interview

When they ask about this project, emphasize:

1. **'Boring infrastructure'** — 'I designed this so deploys are predictable and rollbacks are fast. The canary deployment aborts automatically if error rate exceeds 1%.'

2. **Cost optimization** — 'I used Spot instances and Graviton chips in the Terraform config. The ECR lifecycle policy keeps only the last 30 images. KEDA scales workers to zero when idle.'

3. **Developer experience** — 'The publishctl CLI lets engineers start the entire local stack with one command. Per-PR environments can be spun up with Helm in seconds.'

4. **AI-assisted operations** — 'Instead of manually grepping logs at 2am, the incident analyzer feeds pod state, logs, and events to Claude via Amazon Bedrock and returns a structured report with root cause hypotheses and fix recommendations.'

5. **GitOps discipline** — 'All infrastructure is in Git. ArgoCD is the only thing that touches the cluster. Manual changes are automatically reverted.'

### Keep Learning

After completing this project, deepen your knowledge:

- **Argo Rollouts advanced:** Add Datadog metric analysis (AnalysisTemplate) for automated canary promotion
- **KEDA advanced:** Add AWS SQS scaler with IAM IRSA instead of Redis fallback
- **Terraform advanced:** Add remote state with S3 backend and DynamoDB locking
- **Observability advanced:** Add OpenTelemetry tracing, Sentry release tracking
- **Security:** Add Trivy scanning in CI, OPA Gatekeeper policies in K8s

---

*Good luck with your Buffer application! This project gives you hands-on experience with every major technology in their stack.*
