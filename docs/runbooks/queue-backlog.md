# Queue Backlog Runbook

This runbook covers situations where the queue depth grows beyond acceptable
levels and posts remain stuck in a `queued` state.

---

## Symptoms

- Queue depth metric (`publishhub.queue.depth`) rising steadily
- Posts stay in `queued` state and never transition to `processing` or `published`
- KEDA ScaledObject is not scaling workers up (replicas stay at 0 or minimum)
- Dashboard shows growing `ApproximateNumberOfMessages` (SQS) or `LLEN publishhub:jobs` (Redis)

---

## Diagnosis

### 1. Check queue depth

**Local (Redis):**

```bash
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LLEN publishhub:jobs
```

**AWS (SQS):**

```bash
aws sqs get-queue-attributes \
  --queue-url $(terraform -chdir=terraform output -raw sqs_queue_url) \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

### 2. Check KEDA ScaledObject status

```bash
kubectl get scaledobject -n publishhub
kubectl describe scaledobject publishhub-worker -n publishhub
```

Look for:

- `Ready` condition — if `False`, KEDA cannot read the metric source
- `Active` condition — if `False`, KEDA sees no messages and will not scale
- Recent events showing errors connecting to Redis or SQS

### 3. Check worker pod status

```bash
kubectl get pods -n publishhub -l app=publishhub-worker
kubectl describe pod -n publishhub -l app=publishhub-worker
```

If there are zero pods and the ScaledObject shows `Active: False`, the issue
is that KEDA does not see pending messages. If pods exist but are unhealthy, see
the [worker crash loop runbook](worker-crash-loop.md).

### 4. Check HPA (if present alongside KEDA)

```bash
kubectl get hpa -n publishhub
```

Ensure the HPA and ScaledObject are not conflicting on the same target.

### 5. Check KEDA operator health

```bash
kubectl get pods -n keda
kubectl logs -n keda deploy/keda-operator --tail=50
```

---

## Resolution

### KEDA operator not running

```bash
# Re-install KEDA
make platform-install
```

Or manually:

```bash
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace
```

### KEDA cannot connect to Redis

Verify the `REDIS_ADDRESS` in the ScaledObject metadata matches the actual Redis
service:

```bash
kubectl get svc -n publishhub publishhub-redis
```

Ensure Redis is responding:

```bash
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli PING
```

### KEDA cannot connect to SQS (AWS)

Check the IRSA service account annotation:

```bash
kubectl get serviceaccount -n publishhub publishhub-worker -o yaml
```

Verify the IAM role has `sqs:GetQueueAttributes` permission on the queue ARN.

### Workers running but not processing

Check worker logs for errors:

```bash
kubectl logs -n publishhub -l app=publishhub-worker --tail=100
```

Common causes:

- `QUEUE_BACKEND` mismatch between API and worker
- `REDIS_URL` or `SQS_QUEUE_URL` pointing to wrong endpoint
- Worker stuck in a long-running job (check `SIMULATE_LATENCY_MS`)

### Force scale-up (temporary)

If you need to process a backlog immediately while diagnosing the root cause:

```bash
kubectl scale deployment publishhub-worker -n publishhub --replicas=5
```

Note: KEDA will override this after its next polling interval. To prevent KEDA
from scaling down while you investigate, temporarily set `minReplicaCount` in the
ScaledObject.

---

## Prevention

- Set up an alert on queue depth growing for more than 10 minutes
  (configured in `observability/datadog/monitors.yaml`)
- Monitor the KEDA ScaledObject `Ready` and `Active` conditions
- Keep `pollingInterval` low enough (default 15s) that backlogs are caught early
- Ensure `cooldownPeriod` (default 60s) does not prevent scaling during sustained
  load bursts
- Test scale-to-zero and scale-from-zero paths after any configuration change
