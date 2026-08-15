# Canary Rollout Failure Runbook

This runbook covers situations where a canary deployment via Argo Rollouts
fails, gets stuck, or is auto-aborted.

---

## Symptoms

- Rollout status shows `Degraded` or `Paused` unexpectedly
- Traffic not shifting to the new version
- AnalysisRun shows `Failed` or `Error`
- Old (stable) version is still serving all traffic after an expected promotion
- `kubectl argo rollouts get rollout` shows the canary stuck at a weight step

---

## Diagnosis

### 1. Check rollout status

```bash
kubectl argo rollouts get rollout publishhub-api -n publishhub
```

This shows:

- Current step and weight
- Canary and stable replica counts
- AnalysisRun status
- Whether the rollout is paused or aborted

### 2. Check AnalysisRun results

```bash
kubectl get analysisrun -n publishhub --sort-by='.metadata.creationTimestamp'
kubectl describe analysisrun -n publishhub <latest-analysis-run-name>
```

Look for:

- `status.phase`: `Successful`, `Failed`, or `Error`
- `status.metricResults`: which specific metric caused the failure
- Error messages from the metric provider (Datadog, Job, etc.)

### 3. Check canary pod health

```bash
kubectl get pods -n publishhub -l rollouts-pod-template-hash
kubectl describe pod -n publishhub <canary-pod-name>
kubectl logs -n publishhub <canary-pod-name> --tail=100
```

### 4. Check services (canary vs stable)

```bash
kubectl get svc -n publishhub
kubectl get endpoints -n publishhub publishhub-api-canary
kubectl get endpoints -n publishhub publishhub-api-stable
```

### 5. Check rollout events

```bash
kubectl get events -n publishhub --sort-by='.lastTimestamp' | grep -i rollout
```

---

## Common Failures and Resolutions

### Auto-aborted by AnalysisRun

**Symptom:** Rollout shows `Degraded` with message "AnalysisRun failed".

**Resolution:**

1. Check what metric failed:
   ```bash
   kubectl get analysisrun -n publishhub -o yaml | grep -A 10 'metricResults'
   ```

2. If using the Datadog analysis template, verify:
   - Datadog API keys are correct
   - The query returns data (not empty during low-traffic periods)
   - Error rate thresholds are appropriate

3. If using the Job-based fallback probe (local):
   - Check the probe job logs:
     ```bash
     kubectl logs -n publishhub job/<analysis-job-name>
     ```
   - Verify the health endpoint responds on the canary service

### Canary pods crashing

**Symptom:** Canary pods in `CrashLoopBackOff` while stable pods are fine.

**Resolution:**

- Check the canary pod logs for the new image's errors
- Compare environment variables between canary and stable pods
- Verify the new image was pushed correctly and is pullable
- See the [worker crash loop runbook](worker-crash-loop.md) for pod-level
  debugging

### Rollout stuck at pause step

**Symptom:** Rollout is paused at step 2 (indefinite pause) waiting for
manual promotion.

This is **expected behavior**. The rollout strategy includes an indefinite pause
at 10% traffic to allow manual verification before proceeding.

**Resolution:**

```bash
# Promote to continue the rollout
kubectl argo rollouts promote publishhub-api -n publishhub
```

Or using the CLI shorthand:

```bash
publishctl rollout promote
```

### Traffic not shifting (no service mesh)

**Symptom:** Traffic appears to stay on stable even after weight increase.

Without a service mesh or ingress traffic-router, Argo Rollouts approximates
traffic weight through replica ratios. With few total replicas, the weight
steps may not be granular.

**Resolution:**

- This is a known limitation documented in the design. With 4 total replicas,
  10% weight means 0-1 canary replicas.
- For more precise traffic splitting, integrate a service mesh or supported
  ingress controller.
- For local testing, increase total replicas to make the ratio more visible.

### Image pull failure on canary

**Symptom:** Canary pods stuck in `ImagePullBackOff`.

**Resolution:**

```bash
kubectl describe pod -n publishhub <canary-pod-name> | grep -A 5 'Events'
```

- Verify the image tag exists in the registry
- For ECR, confirm node IAM permissions
- For local registry, confirm the kind cluster can reach `localhost:5001`

---

## Recovery

### Abort to stable

If the canary is unhealthy and you want to immediately route all traffic back
to the stable version:

```bash
kubectl argo rollouts abort publishhub-api -n publishhub
```

Or:

```bash
publishctl rollout abort
```

After abort, the stable version handles 100% of traffic. The rollout enters
`Degraded` state until a new revision is deployed.

### Retry after fixing the image

After fixing the issue and pushing a corrected image:

```bash
# Update the image tag in values and push to Git
# ArgoCD will detect the change and create a new rollout revision

# Or manually set a new image:
kubectl argo rollouts set image publishhub-api \
  api=<registry>/publishhub-api:<new-tag> -n publishhub
```

### Force full promotion (skip remaining steps)

If you are confident the canary is healthy and want to skip remaining pause
steps:

```bash
kubectl argo rollouts promote publishhub-api --full -n publishhub
```

Use with caution — this bypasses remaining analysis steps.

---

## Prevention

- Test new images locally with `docker-compose` before pushing
- Run the CI pipeline (lint, typecheck, unit tests, Trivy scan) before deploying
- Set appropriate thresholds in the AnalysisTemplate that account for normal
  variance
- Keep the indefinite pause step (step 2) so you can manually verify the canary
  at low traffic before committing
- Monitor the rollout dashboard and set alerts on `Degraded` status
- Ensure analysis metrics have data before deploying — empty metrics can cause
  false positives or false negatives
