# ArgoCD Sync Failure Runbook

This runbook covers situations where ArgoCD cannot synchronize the desired state
from Git to the cluster, leaving applications degraded or out-of-sync.

---

## Symptoms

- ArgoCD Application shows `Degraded` or `OutOfSync` status
- Pods not updating after a Git push
- `kubectl get applications -n argocd` shows sync errors
- ArgoCD UI shows red or yellow status indicators
- Manual changes on the cluster are being reverted unexpectedly (self-heal
  working correctly but unexpected)

---

## Diagnosis

### 1. Check Application status

```bash
kubectl get applications -n argocd
kubectl get applications -n argocd publishhub -o yaml
```

Look for:

- `status.sync.status`: should be `Synced`
- `status.health.status`: should be `Healthy`
- `status.conditions`: contains error messages

### 2. Check sync operation details

```bash
kubectl get applications -n argocd publishhub \
  -o jsonpath='{.status.operationState.message}'
```

### 3. Check ArgoCD logs

```bash
kubectl logs -n argocd deploy/argocd-application-controller --tail=100
kubectl logs -n argocd deploy/argocd-repo-server --tail=100
```

### 4. Check recent events

```bash
kubectl get events -n argocd --sort-by='.lastTimestamp' --field-selector reason!=Synced
```

### 5. Verify Helm template renders cleanly

Before blaming ArgoCD, confirm the chart templates are valid:

```bash
helm template publishhub helm/publishhub/ -f helm/publishhub/values.yaml
helm template publishhub helm/publishhub/ -f helm/publishhub/values-production.yaml
```

---

## Common Failures and Resolutions

### Image pull errors

**Symptom:** Pods stuck in `ImagePullBackOff`.

```bash
kubectl get events -n publishhub --field-selector reason=Failed | grep -i pull
```

**Resolution:**

- Verify the image exists in the registry:
  ```bash
  # Local
  curl -s http://localhost:5001/v2/_catalog

  # ECR
  aws ecr describe-images --repository-name publishhub-api --region us-east-1
  ```
- Confirm the image tag in `values.yaml` or `values-production.yaml` matches a
  pushed image
- For ECR, verify the node's IAM role has `ecr:GetAuthorizationToken` and
  `ecr:BatchGetImage` permissions

### Helm template errors

**Symptom:** ArgoCD shows `ComparisonError` in the Application status.

**Resolution:**

- Run `helm lint helm/publishhub/` locally to catch syntax errors
- Check for missing required values (the chart uses `{{ required }}` for
  mandatory fields)
- Verify the Helm chart version in `Chart.yaml` matches what ArgoCD expects

### Namespace does not exist

**Symptom:** `namespace "publishhub" not found` in sync errors.

**Resolution:**

```bash
kubectl create namespace publishhub
```

Or ensure the ArgoCD Application has `syncPolicy.syncOptions` including
`CreateNamespace=true`.

### RBAC / permission errors

**Symptom:** `forbidden` errors in the controller logs.

**Resolution:**

- Check the ArgoCD AppProject allows the resource kinds being deployed:
  ```bash
  kubectl get appproject -n argocd publishhub -o yaml
  ```
- Verify `clusterResourceWhitelist` includes any cluster-scoped resources
  (like `ClusterRole` or `Namespace`)

### Resource conflict (already managed by another controller)

**Symptom:** `the object has been modified` or ownership conflict errors.

**Resolution:**

- Delete the conflicting resource and let ArgoCD recreate it
- Or add the `argocd.argoproj.io/managed-by` annotation to claim ownership

### Git repository unreachable

**Symptom:** `rpc error` or `repository not accessible` in repo-server logs.

**Resolution:**

```bash
kubectl logs -n argocd deploy/argocd-repo-server --tail=50 | grep -i error
```

- Verify the repository URL is correct in the Application spec
- Check network connectivity from the cluster to GitHub
- For private repos, verify SSH keys or access tokens in the ArgoCD repo config

---

## Recovery

### Force a manual sync

```bash
# Using kubectl
kubectl patch application publishhub -n argocd \
  --type merge -p '{"operation": {"initiatedBy": {"username": "admin"}, "sync": {"revision": "HEAD"}}}'
```

Or using the ArgoCD CLI:

```bash
argocd app sync publishhub
```

### Force a refresh (re-read from Git)

```bash
argocd app get publishhub --refresh
```

Or annotate the Application to trigger a refresh:

```bash
kubectl annotate application publishhub -n argocd \
  argocd.argoproj.io/refresh=normal --overwrite
```

### Hard refresh (clear cache)

If ArgoCD is using a stale cached version of the repo:

```bash
argocd app get publishhub --hard-refresh
```

### Resolve conflicts by deleting and re-syncing

If a resource is in a conflicted state that ArgoCD cannot reconcile:

```bash
# Delete the specific conflicting resource
kubectl delete deployment publishhub-api -n publishhub

# ArgoCD self-heal will recreate it from Git
```

---

## Prevention

- Run `helm lint` and `helm template` in CI before merging (configured in
  `.github/workflows/ci.yaml`)
- Keep ArgoCD's `selfHeal: true` and `prune: true` enabled so drift is
  auto-corrected
- Use `argocd app diff publishhub` to preview what a sync will change
- Monitor the ArgoCD Application health and set alerts on `Degraded` status
- Ensure the App of Apps pattern keeps the repository URL in a single place so
  forks only need one edit
- Test Helm value changes with `helm template` locally before pushing
