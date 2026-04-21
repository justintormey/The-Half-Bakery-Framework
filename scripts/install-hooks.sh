#!/usr/bin/env bash
# Install tracked git hooks from .githooks/ into .git/hooks/
# Run once after cloning: bash scripts/install-hooks.sh
#
# This installs a pre-commit hook that blocks accidental PII commits.
# See .githooks/pre-commit for the patterns blocked — edit that file
# to configure your own private infrastructure values before installing.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [[ -z "$REPO_ROOT" ]]; then
    echo "ERROR: must be run from inside the Half Bakery Framework git repo" >&2
    exit 1
fi

HOOKS_SRC="${REPO_ROOT}/.githooks"
HOOKS_DST="${REPO_ROOT}/.git/hooks"

if [[ ! -d "$HOOKS_SRC" ]]; then
    echo "ERROR: .githooks/ directory not found at ${HOOKS_SRC}" >&2
    exit 1
fi

echo "Installing git hooks from .githooks/ → .git/hooks/"

for hook_file in "${HOOKS_SRC}"/*; do
    hook_name="$(basename "$hook_file")"
    dest="${HOOKS_DST}/${hook_name}"

    if [[ -f "$dest" && ! -L "$dest" ]]; then
        echo "  Backing up existing ${hook_name} → ${hook_name}.bak"
        cp "$dest" "${dest}.bak"
    fi

    cp "$hook_file" "$dest"
    chmod +x "$dest"
    echo "  ✓ Installed ${hook_name}"
done

echo ""
echo "Done. Hooks active for all commits in this repo and its worktrees."
echo "To test: run \`bash .githooks/pre-commit\` (should exit 0 with no staged PII)"
echo ""
echo "REMINDER: Edit .githooks/pre-commit to replace placeholder patterns"
echo "  (YOUR_LAN_IP, YOUR_S3_BUCKET/) with your real private values."
