#!/usr/bin/env bash
# Re-vendor the Agent Skills from the canonical repo. See
# ietf_llm/data/skills/VENDORED.md.
#
# Prefer the make targets, which is how CI invokes this:
#
#   make vendor-skills [REF=v0.4.1]   -> this script [REF]
#   make vendor-skills-check          -> this script --check
#
#   scripts/vendor-skills.sh [REF]
#     Vendor every skill upstream publishes and rewrite the tag+commit pin in
#     VENDORED.md — no hand-copying. With no REF, tracks the newest vN.N.N tag
#     upstream; pass a tag to pin a specific version (e.g. v0.2.2). Review the
#     diff, then commit.
#
#   scripts/vendor-skills.sh --check
#     Verify the on-disk vendored skills (and the pin recorded in VENDORED.md)
#     match upstream at that pinned tag. Runs in CI on every push.
#
#     Exit 0  everything matches the pin.
#     Exit 1  something is wrong and we know it: file or skill-set drift, or a
#             pin GitHub says does not resolve. Fails the build.
#     Exit 2  we could not find out — no network, `gh` missing, a 5xx. Warns
#             only. Note that 2 is non-zero without meaning drift, so callers
#             must test the value rather than mere success (`make` cannot: it
#             collapses any recipe failure to its own exit 2, which is why the
#             make target translates rather than the workflow).
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

resolve_sha() {  # ref
  if ! gh api "repos/$REPO/commits/$1" --jq '.sha' 2> "$work/gh.err"; then
    cat "$work/gh.err" >&2
    return "$(gh_failure_kind "$work/gh.err")"
  fi
}

# Exit statuses, shared by the helpers below and by --check.
#   1 -- a definitive answer that something is wrong (drift, or a pin GitHub
#        says does not exist). Fails the build.
#   2 -- we could not find out. Warns; does not fail the build.
readonly WRONG=1
readonly UNKNOWN=2

# Classify a failed `gh` call from its stderr. An HTTP 4xx is GitHub answering
# the question -- the tag is gone, the repo was renamed, it went private -- and
# a pin that cannot be resolved is drift of the worst kind, since re-vendoring
# from it is impossible. Anything else (no route, TLS, 5xx, `gh` not installed)
# is us failing to ask.
gh_failure_kind() {  # stderr-file
  grep -qE 'HTTP 4[0-9][0-9]' "$1" && echo "$WRONG" || echo "$UNKNOWN"
}

# Extract the repo at $1 into $work/src and echo the extracted root. A tarball
# rather than per-file content calls: it is one request whatever the skill
# count, and it carries subdirectories without walking the tree API.
#
# Fails at the step that actually failed. Letting a 404 fall through to `tar`
# and then to an empty-root guard produced three messages for one fault, the
# last of which ("empty tarball") named a cause that was not the cause.
fetch_tree() {  # ref
  local src="$work/src"
  mkdir -p "$src"
  if ! gh api "repos/$REPO/tarball/$1" > "$work/repo.tar.gz" 2> "$work/gh.err"; then
    cat "$work/gh.err" >&2
    return "$(gh_failure_kind "$work/gh.err")"
  fi
  if ! tar xzf "$work/repo.tar.gz" -C "$src" 2>/dev/null; then
    echo "$REPO@$1 did not return a tarball" >&2
    return "$UNKNOWN"
  fi
  # GitHub's tarball wraps everything in one <owner>-<repo>-<sha> directory.
  local root
  root="$(find "$src" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [[ -n "$root" ]] || { echo "empty tarball for $REPO@$1" >&2; return "$UNKNOWN"; }
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
  set +e
  root="$(fetch_tree "$ref")"
  fetched=$?
  set -e
  if [[ $fetched -eq $WRONG ]]; then
    echo "DRIFT: VENDORED.md pins $REPO@$ref, which does not resolve" >&2
    exit "$WRONG"
  elif [[ $fetched -ne 0 ]]; then
    echo "could not reach $REPO@$ref; vendored skills not checked" >&2
    exit "$UNKNOWN"
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
  set +e
  actual="$(resolve_sha "$ref")"
  resolved=$?
  set -e
  if [[ $resolved -eq $WRONG ]]; then
    echo "DRIFT: VENDORED.md pins $REPO@$ref, which does not resolve" >&2
    exit "$WRONG"
  elif [[ $resolved -ne 0 ]]; then
    echo "could not resolve $REPO@$ref; vendored skills not checked" >&2
    exit "$UNKNOWN"
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
