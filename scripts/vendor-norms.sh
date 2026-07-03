#!/usr/bin/env bash
# Re-vendor the norm skills (ietf-contributing, ietf-interpreting) from the
# canonical repo at a pinned tag. See ietf_llm/data/skills/VENDORED.md.
#
# Bump VENDOR_REF deliberately when the upstream norm text changes, run this,
# update the commit SHA in VENDORED.md to the printed value, and commit.
set -euo pipefail

VENDOR_REF="v0.2.0"
REPO="mnot/ietf-skill"

here="$(cd "$(dirname "$0")/.." && pwd)"
dest="$here/ietf_llm/data/skills"

for skill in ietf-contributing ietf-interpreting; do
  gh api "repos/$REPO/contents/$skill/SKILL.md?ref=$VENDOR_REF" --jq '.content' \
    | base64 -d > "$dest/$skill/SKILL.md"
  echo "vendored $skill/SKILL.md from $REPO@$VENDOR_REF"
done

sha="$(gh api "repos/$REPO/commits/$VENDOR_REF" --jq '.sha')"
echo "pinned commit: $sha"
echo "-> update the commit SHA in $dest/VENDORED.md if it changed."
