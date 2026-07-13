#!/usr/bin/env bash
# Re-vendor the norm skills (ietf-contributing, ietf-interpreting) from the
# canonical repo. See ietf_llm/data/skills/VENDORED.md.
#
#   scripts/vendor-norms.sh [REF]
#     Vendor the norm SKILL.md bodies and rewrite the tag+commit pin in
#     VENDORED.md — no hand-copying. With no REF, tracks the newest vN.N.N tag
#     upstream; pass a tag to pin a specific version (e.g. v0.2.2). Review the
#     diff, then commit.
#
#   scripts/vendor-norms.sh --check
#     Verify the on-disk vendored files (and the pin recorded in VENDORED.md)
#     match upstream at that pinned tag. Exits non-zero on any drift. Run before
#     a release, or wire into CI.
set -euo pipefail

REPO="mnot/ietf-skill"
SKILLS=(ietf-contributing ietf-interpreting)

here="$(cd "$(dirname "$0")/.." && pwd)"
dest="$here/ietf_llm/data/skills"
vendored_md="$dest/VENDORED.md"

# Newest vN.N.N tag upstream (semver-sorted, so API order doesn't matter).
resolve_latest() {
  gh api "repos/$REPO/tags" --jq '.[].name' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -V | tail -1
}

# The tag currently pinned in VENDORED.md.
current_ref() {
  grep -oE 'tag v[0-9]+\.[0-9]+\.[0-9]+' "$vendored_md" | head -1 | awk '{print $2}'
}

# The commit SHA currently pinned in VENDORED.md.
current_sha() {
  grep -oE 'commit [0-9a-f]+' "$vendored_md" | head -1 | awk '{print $2}'
}

resolve_sha() { gh api "repos/$REPO/commits/$1" --jq '.sha'; }

# Write a skill's SKILL.md at $ref to $out.
fetch_skill() {  # ref skill out
  gh api "repos/$REPO/contents/$2/SKILL.md?ref=$1" --jq '.content' | base64 -d > "$3"
}

# Rewrite the "tag vX.Y.Z  (commit <sha>)" line in VENDORED.md in place
# (temp-file + mv, so it works on both BSD/macOS and GNU sed).
update_vendored_md() {  # ref sha
  sed -E "s|tag v[0-9]+\.[0-9]+\.[0-9]+ +\(commit [0-9a-f]+\)|tag $1  (commit $2)|" \
    "$vendored_md" > "$vendored_md.tmp"
  mv "$vendored_md.tmp" "$vendored_md"
}

if [[ "${1:-}" == "--check" ]]; then
  ref="$(current_ref)"
  [[ -n "$ref" ]] || { echo "could not read a pinned tag from VENDORED.md" >&2; exit 1; }
  status=0
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  for skill in "${SKILLS[@]}"; do
    fetch_skill "$ref" "$skill" "$tmp/$skill.md"
    if ! diff -q "$tmp/$skill.md" "$dest/$skill/SKILL.md" >/dev/null; then
      echo "DRIFT: $skill/SKILL.md differs from $REPO@$ref" >&2
      status=1
    fi
  done
  recorded="$(current_sha)"
  actual="$(resolve_sha "$ref")"
  if [[ "$recorded" != "$actual" ]]; then
    echo "DRIFT: VENDORED.md pins commit $recorded but $REPO@$ref is $actual" >&2
    status=1
  fi
  [[ $status -eq 0 ]] && echo "vendored norms match $REPO@$ref"
  exit $status
fi

ref="${1:-$(resolve_latest)}"
[[ -n "$ref" ]] || { echo "could not resolve a tag from $REPO" >&2; exit 1; }
sha="$(resolve_sha "$ref")"

for skill in "${SKILLS[@]}"; do
  fetch_skill "$ref" "$skill" "$dest/$skill/SKILL.md"
  echo "vendored $skill/SKILL.md from $REPO@$ref"
done
update_vendored_md "$ref" "$sha"
echo "pinned $REPO@$ref ($sha) in VENDORED.md"
