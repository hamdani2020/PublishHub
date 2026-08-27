#!/usr/bin/env bash
# Sets up branch protection rules for the main branch.
# Requires: gh CLI authenticated with admin access to the repository.
#
# This enforces:
#   - No force pushes or branch deletion
#   - CI runs on PRs via workflow trigger (not branch protection)
#   - Deploy bot can push image tag commits directly
#
# Note: On GitHub Free, the GITHUB_TOKEN cannot bypass PR requirements
# or required status checks. So we don't enforce those at the branch
# protection level. Instead, CI is enforced by the workflow triggers
# (CI runs on pull_request events) and team convention.
#
# Usage:
#   ./scripts/setup-branch-protection.sh

set -euo pipefail

REPO="hamdani2020/PublishHub"
BRANCH="main"

echo "Configuring branch protection for ${REPO}@${BRANCH}..."

gh api \
  --method PUT \
  "/repos/${REPO}/branches/${BRANCH}/protection" \
  --input - <<'EOF'
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF

echo ""
echo "Done. Branch protection configured for ${BRANCH}:"
echo "  - No force pushes"
echo "  - No branch deletion"
echo "  - Direct pushes allowed (needed for deploy bot)"
echo ""
echo "CI enforcement is handled by workflow triggers:"
echo "  - ci.yaml runs on pull_request → validates before merge"
echo "  - deploy.yaml runs on push to main → builds and deploys"
echo "  - Convention: always use PRs for code changes"
