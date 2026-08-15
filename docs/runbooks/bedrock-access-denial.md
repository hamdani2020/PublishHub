# Bedrock Access Denial Runbook

This runbook covers situations where the AI incident analyzer
(`scripts/ai-incident-analyzer.py`) fails due to AWS Bedrock access or
credential issues.

---

## Symptoms

- Incident analyzer exits with a non-zero code and an access-related error
- Error messages mention `AccessDeniedException`, `ValidationException`,
  `ExpiredTokenException`, or `ThrottlingException`
- The analyzer collects pod data successfully but fails during the AI analysis
  step

---

## Diagnosis

### 1. Verify AWS credentials are valid

```bash
aws sts get-caller-identity
```

If this fails, your credentials are missing or expired.

### 2. Verify the Bedrock region

Claude 3 Haiku is not available in all regions. The default is `us-east-1`.
Check which region the analyzer is targeting:

```bash
echo $AWS_REGION
# or
echo $AWS_DEFAULT_REGION
```

### 3. Check model access in the Bedrock console

Open the AWS Console:

1. Navigate to **Amazon Bedrock** > **Model access**
2. Confirm Claude 3 Haiku (`anthropic.claude-3-haiku-20240307-v1:0`) shows
   **Access granted**

If not, you need to request access (see Resolution below).

### 4. Test Bedrock access directly

```bash
aws bedrock-runtime invoke-model \
  --model-id anthropic.claude-3-haiku-20240307-v1:0 \
  --region us-east-1 \
  --content-type application/json \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
  /tmp/bedrock-test.json && cat /tmp/bedrock-test.json
```

---

## Error-to-Resolution Map

### AccessDeniedException

**Message:** "Model access not granted"

**Cause:** The Claude 3 Haiku model has not been enabled for your AWS account
in the target region.

**Resolution:**

1. Open the AWS Console for the target region (default: `us-east-1`)
2. Navigate to **Amazon Bedrock** > **Model access** (left sidebar)
3. Click **Manage model access**
4. Find **Anthropic** > **Claude 3 Haiku** and check the box
5. Click **Request model access**
6. Access is typically granted within a few minutes

Alternatively, your IAM principal may lack the `bedrock:InvokeModel` permission:

```bash
# Check if the issue is IAM policy vs model access
aws bedrock list-foundation-models --region us-east-1 --query 'modelSummaries[?modelId==`anthropic.claude-3-haiku-20240307-v1:0`]'
```

If this command also fails with `AccessDeniedException`, the IAM policy needs
updating. Required permissions:

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:InvokeModel",
    "bedrock:ListFoundationModels"
  ],
  "Resource": "*"
}
```

### ValidationException (model ID)

**Message:** "Model unavailable in region"

**Cause:** The model ID is invalid or the model is not available in the
specified region.

**Resolution:**

- Use a region where Claude 3 Haiku is available. Confirmed regions include:
  `us-east-1`, `us-west-2`, `eu-west-1`
- Set the region explicitly:
  ```bash
  export AWS_REGION=us-east-1
  ```
- Verify the model ID hasn't changed:
  ```bash
  aws bedrock list-foundation-models --region us-east-1 \
    --query 'modelSummaries[?contains(modelId, `claude-3-haiku`)].[modelId]' \
    --output text
  ```

### ExpiredTokenException / NoCredentialsError

**Message:** "AWS credentials missing or expired"

**Cause:** No valid AWS credentials in the environment.

**Resolution:**

For **IAM user credentials:**

```bash
aws configure
# Enter Access Key ID, Secret Access Key, and region
```

For **SSO sessions:**

```bash
aws sso login --profile <your-profile>
```

For **temporary credentials (STS):**

```bash
# Check when they expire
aws sts get-caller-identity
# If expired, refresh from your identity provider
```

For **environment variable credentials:**

```bash
# Verify they are set
echo $AWS_ACCESS_KEY_ID
echo $AWS_SECRET_ACCESS_KEY
echo $AWS_SESSION_TOKEN  # required for temporary credentials
```

### ThrottlingException

**Message:** "Rate exceeded"

**Cause:** Too many requests to the Bedrock API in a short period.

**Resolution:**

- Wait and retry. The analyzer includes exponential backoff.
- If you are running many analyses in sequence, add a delay between invocations.
- Check your account's Bedrock service quotas:
  ```bash
  aws service-quotas get-service-quota \
    --service-code bedrock \
    --quota-code <quota-code> \
    --region us-east-1
  ```
- Request a quota increase through the AWS Console if needed for sustained use.

---

## Resolution Summary

| Error | Quick Fix |
|-------|-----------|
| `AccessDeniedException` | Enable Claude 3 Haiku in Bedrock console for target region |
| `ValidationException` (model) | Switch to `us-east-1` with `export AWS_REGION=us-east-1` |
| `ExpiredTokenException` | Run `aws sso login` or `aws configure` |
| `NoCredentialsError` | Run `aws configure` or set `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` |
| `ThrottlingException` | Wait and retry; check service quotas |

---

## Prevention

- Verify Bedrock access works before relying on the incident analyzer in an
  actual incident: run `publishctl doctor` which checks prerequisites
- Keep AWS credentials fresh — use SSO with auto-refresh where possible
- Document which region the team uses for Bedrock so everyone has model access
  enabled in the same region
- The analyzer uses the ambient AWS credential chain only — no API keys are
  stored in the repository
- Set `AWS_REGION` in your shell profile to avoid region mismatches
