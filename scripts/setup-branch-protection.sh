#!/usr/bin/env bash
# Sets up branch protection rules for the main branch.
# Requires: gh CLI authenticated with admin access to the repository.
#
# This enforces:
#   - All changes to main go through a PR (CI runs on PR)
#   - No force pushes or branch deletion
#   - Deploy bot can push image tag commits directly (no status check gate)
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
  "required_pull_request_reviews": {
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF

echo ""
echo "Done. Branch protection configured for ${BRANCH}:"
echo "  - PRs required to merge (0 approvals needed, CI runs on PR)"
echo "  - No required status checks on push (deploy bot can push)"
echo "  - Force pushes: disabled"
echo "  - Deletions: disabled"
echo ""
echo "Workflow:"
echo "  1. Push to a feature branch"
echo "  2. Open PR to main → CI runs automatically"
echo "  3. Merge PR → Deploy triggers"
echo "  4. Deploy bot pushes image tag commit → not blocked"
