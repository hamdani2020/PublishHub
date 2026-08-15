# Worker Crash Loop Runbook

This runbook covers situations where the worker pod enters `CrashLoopBackOff`
or repeatedly restarts, preventing job processing.

---

## Symptoms

- Pod status shows `CrashLoopBackOff` or frequent restarts
- Pod terminated with `OOMKilled` reason
- Jobs are not being processed; queue depth growing
- Worker logs show repeated startup/shutdown cycles
- Events show `BackOff` or `Unhealthy` conditions

---

## Diagnosis

### 1. Check pod status and events

```bash
kubectl get pods -n publishhub -l app=publishhub-worker
kubectl describe pod -n publishhub -l app=publishhub-worker
```

Look at the `Last State` section for:

- **Exit code 137**: `OOMKilled` — the container exceeded its memory limit
- **Exit code 1**: Application error on startup
- **Exit code 0 with restarts**: Pod exiting immediately (misconfiguration)

### 2. Check current and previous container logs

```bash
# Current attempt logs
kubectl logs -n publishhub -l app=publishhub-worker --tail=100

# Previous (crashed) container logs
kubectl logs -n publishhub -l app=publishhub-worker --previous --tail=100
```

### 3. Check pod events

```bash
kubectl get events -n publishhub --sort-by='.lastTimestamp' | grep worker
```

### 4. Check resource consumption

```bash
kubectl top pod -n publishhub -l app=publishhub-worker
```

Compare actual usage against the configured limits in the Helm values.

### 5. Check for poison messages

A poison message (unparseable or triggering an unhandled error) can cause the
worker to crash on every receive cycle.

**Redis:**

```bash
# Check DLQ for dead-lettered messages
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LLEN publishhub:jobs:dlq
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LRANGE publishhub:jobs:dlq 0 5

# Check if a message is stuck in the processing list
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LLEN publishhub:jobs:processing
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LRANGE publishhub:jobs:processing 0 5
```

**SQS:**

```bash
aws sqs get-queue-attributes \
  --queue-url $(terraform -chdir=terraform output -raw sqs_dlq_url) \
  --attribute-names ApproximateNumberOfMessages
```

---

## Resolution

### OOMKilled — increase memory limits

The worker is exceeding its configured memory limit. Update `values.yaml` or
`values-production.yaml`:

```yaml
worker:
  resources:
    limits:
      memory: "512Mi"  # increase from default
    requests:
      memory: "256Mi"
```

Apply the change and let ArgoCD sync, or manually:

```bash
kubectl rollout restart deployment/publishhub-worker -n publishhub
```

### Redis connectivity failure

The worker crashes if it cannot reach Redis at startup. Verify Redis is running:

```bash
kubectl get pods -n publishhub -l app=publishhub-redis
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli PING
```

Verify the `REDIS_URL` in the worker's environment:

```bash
kubectl get deployment publishhub-worker -n publishhub -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -m json.tool
```

### Poison messages causing crash

If the worker crashes processing a specific message, the message returns to the
queue (Redis `processing` list) and crashes the worker again on the next
attempt.

Clear the stuck message from the processing list:

```bash
# Inspect the stuck message
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LRANGE publishhub:jobs:processing 0 0

# Move it to the DLQ manually
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli RPOPLPUSH publishhub:jobs:processing publishhub:jobs:dlq
```

### SIMULATE_FAILURE_RATE set too high

If `SIMULATE_FAILURE_RATE` is set close to 1.0, the worker will fail most jobs
and may trigger max-retry exhaustion rapidly:

```bash
kubectl get configmap -n publishhub publishhub-config -o yaml | grep SIMULATE_FAILURE_RATE
```

Set it to `0` for normal operation.

### Missing or invalid environment variables

If a required config variable is missing, the worker fails fast on startup.
Check logs for `missing required configuration` messages:

```bash
kubectl logs -n publishhub -l app=publishhub-worker --tail=20
```

Verify the ConfigMap and any Secrets referenced by the deployment are present.

---

## Recovery

### Restart the worker after fixing the issue

```bash
kubectl rollout restart deployment/publishhub-worker -n publishhub
kubectl rollout status deployment/publishhub-worker -n publishhub
```

### Clear the DLQ after investigation

Once you have inspected and addressed the root cause of dead-lettered messages:

**Redis:**

```bash
# View messages before clearing
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli LRANGE publishhub:jobs:dlq 0 -1

# Clear the DLQ
kubectl exec -n publishhub deploy/publishhub-redis -- redis-cli DEL publishhub:jobs:dlq
```

**SQS:**

```bash
aws sqs purge-queue --queue-url $(terraform -chdir=terraform output -raw sqs_dlq_url)
```

---

## Prevention

- Set resource limits that account for peak job sizes (large content fields)
- Monitor pod restart counts — alert on more than 3 restarts in 15 minutes
  (configured in `observability/datadog/monitors.yaml`)
- Keep `SIMULATE_FAILURE_RATE` at 0 in production
- Implement schema validation that dead-letters unparseable messages immediately
  rather than crashing (already in the worker design)
- Test the worker's graceful shutdown path after configuration changes
- Review DLQ contents periodically to identify recurring problem patterns
