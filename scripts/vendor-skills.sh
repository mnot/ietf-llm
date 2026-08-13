#!/usr/bin/env bash
# Re-vendor the Agent Skills from the canonical repo. See
# ietf_llm/data/skills/VENDORED.md.
#
#   scripts/vendor-skills.sh [REF]
#     Vendor every skill upstream publishes and rewrite the tag+commit pin in
#     VENDORED.md — no hand-copying. With no REF, tracks the newest vN.N.N tag
#     upstream; pass a tag to pin a specific version (e.g. v0.2.2). Review the
#     diff, then commit.
#
#   scripts/vendor-skills.sh --check
#     Verify the on-disk vendored skills (and the pin recorded in VENDORED.md)
#     match upstream at that pinned tag. Exits non-zero on any drift. Run before
#     a release, or wire into CI.
#
# The skill set is *discovered*, not listed here: every top-level directory
# upstream that holds a SKILL.md is vendored, whole tree (several carry
# reference/ subdirectories and companion .md files, not just SKILL.md). So a
# skill added upstream arrives on the next re-vendor without editing this
# script, and one retired upstream is pruned locally.
set -euo pipefail

REPO="mnot/ietf-skill"

here="$(cd "$(dirname "$0")/.." && pwd)"
dest="$here/ietf_llm/data/skills"
vendored_md="$dest/VENDORED.md"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

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

# Extract the repo at $1 into $work/src and echo the extracted root. A tarball
# rather than per-file content calls: it is one request whatever the skill
# count, and it carries subdirectories without walking the tree API.
fetch_tree() {  # ref
  local src="$work/src"
  mkdir -p "$src"
  gh api "repos/$REPO/tarball/$1" > "$work/repo.tar.gz"
  tar xzf "$work/repo.tar.gz" -C "$src"
  # GitHub's tarball wraps everything in one <owner>-<repo>-<sha> directory.
  local root
  root="$(find "$src" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [[ -n "$root" ]] || { echo "empty tarball for $REPO@$1" >&2; exit 1; }
  echo "$root"
}

# Skill directory names under $1, sorted — every top-level dir with a SKILL.md.
skills_in() {  # root
  local dir
  for dir in "$1"/*/; do
    [[ -f "$dir/SKILL.md" ]] && basename "$dir"
  done | sort
}

# Local skills we have vendored, sorted.
local_skills() {
  local dir
  for dir in "$dest"/*/; do
    [[ -f "$dir/SKILL.md" ]] && basename "$dir"
  done | sort
}

# Every directory under $dest, sorted — including any without a SKILL.md.
# `local_skills` cannot see those, but `data/skills/**/*` still ships them, so
# --check has to look wider than the vendoring does. An interrupted run between
# the rm -rf and the cp -R leaves exactly that.
local_dirs() {
  find "$dest" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort
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
  # Exit 2, distinct from the exit 1 that means drift: a check that reports
  # "upstream had a bad minute" the same way it reports "the vendored files are
  # wrong" is one people learn to re-run rather than read, and then it is not a
  # check at all. CI turns 2 into a warning.
  if ! root="$(fetch_tree "$ref")"; then
    echo "could not reach $REPO@$ref; vendored skills not checked" >&2
    exit 2
  fi
  status=0
  upstream="$(skills_in "$root")"
  mine="$(local_dirs)"
  if [[ "$upstream" != "$mine" ]]; then
    echo "DRIFT: vendored skill set differs from $REPO@$ref" >&2
    diff <(echo "$mine") <(echo "$upstream") | sed 's/^/  /' >&2
    status=1
  fi
  for skill in $upstream; do
    [[ -d "$dest/$skill" ]] || continue  # already reported as a set difference
    if ! diff -r "$root/$skill" "$dest/$skill" >/dev/null; then
      echo "DRIFT: $skill/ differs from $REPO@$ref" >&2
      status=1
    fi
  done
  recorded="$(current_sha)"
  if ! actual="$(resolve_sha "$ref")"; then
    echo "could not resolve $REPO@$ref; vendored skills not checked" >&2
    exit 2
  fi
  if [[ "$recorded" != "$actual" ]]; then
    echo "DRIFT: VENDORED.md pins commit $recorded but $REPO@$ref is $actual" >&2
    status=1
  fi
  [[ $status -eq 0 ]] && echo "vendored skills match $REPO@$ref"
  exit $status
fi

ref="${1:-$(resolve_latest)}"
[[ -n "$ref" ]] || { echo "could not resolve a tag from $REPO" >&2; exit 1; }
sha="$(resolve_sha "$ref")"
root="$(fetch_tree "$ref")"

upstream="$(skills_in "$root")"
[[ -n "$upstream" ]] || { echo "no skills found in $REPO@$ref" >&2; exit 1; }

for skill in $upstream; do
  rm -rf "${dest:?}/$skill"
  cp -R "$root/$skill" "$dest/$skill"
  echo "vendored $skill/ from $REPO@$ref"
done

# Anything we hold that upstream no longer publishes. Dropping it here is what
# lets `--install-skills` stop shipping a retired skill; removing the installed
# copy on a user's machine is `skill_install._prune_orphans`' job. Over every
# directory, not just the ones with a SKILL.md, so a torn earlier run cleans up
# rather than shipping its debris in the wheel.
for skill in $(local_dirs); do
  if ! echo "$upstream" | grep -qx "$skill"; then
    rm -rf "${dest:?}/$skill"
    echo "pruned $skill/ — no longer published by $REPO"
  fi
done

update_vendored_md "$ref" "$sha"
echo "pinned $REPO@$ref ($sha) in VENDORED.md"
