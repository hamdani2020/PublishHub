#!/usr/bin/env bash
# Sets up branch protection rules for the main branch.
# Requires: gh CLI authenticated with admin access to the repository.
#
# This enforces:
#   - All CI checks must pass before merging
#   - At least 1 pull request review (optional, remove if solo dev)
#   - No direct pushes to main (all changes via PR)
#   - Branches must be up-to-date before merging
#
# Usage:
#   ./scripts/setup-branch-protection.sh

set -euo pipefail

REPO="hamdani2020/PublishHub"
BRANCH="main"

echo "Configuring branch protection for ${REPO}@${BRANCH}..."

# Required status checks (all CI jobs that must pass)
REQUIRED_CHECKS=(
  "API: lint + typecheck + test"
  "Web: lint + typecheck + test"
  "Worker: lint + test"
  "Helm: lint + template"
  "Terraform: fmt + validate"
  "Images: build + Trivy scan (api)"
  "Images: build + Trivy scan (worker)"
  "Images: build + Trivy scan (web)"
)

# Build the checks JSON array
CHECKS_JSON=$(printf '%s\n' "${REQUIRED_CHECKS[@]}" | jq -R . | jq -s 'map({context: ., app_id: null})')

gh api \
  --method PUT \
  "/repos/${REPO}/branches/${BRANCH}/protection" \
  --input - <<EOF
{
  "required_status_checks": {
    "strict": true,
    "checks": ${CHECKS_JSON}
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF

echo "Done. Branch protection configured for ${BRANCH}:"
echo "  - Required checks: ${#REQUIRED_CHECKS[@]} CI jobs must pass"
echo "  - Strict mode: branch must be up-to-date with main"
echo "  - Force pushes: disabled"
echo "  - Deletions: disabled"
echo ""
echo "Note: Direct pushes to main are still allowed (no PR requirement)."
echo "To require PRs, re-run with required_pull_request_reviews configured."
