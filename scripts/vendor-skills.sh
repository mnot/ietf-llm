#!/usr/bin/env bash
# Re-vendor the Agent Skills from the canonical repo. See
# ietf_llm/data/skills/VENDORED.md.
#
# Needs only `git` and `curl` (see below) — no `gh`, no token.
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
#             pin upstream does not have. Fails the build.
#     Exit 2  we could not find out — no network, a 5xx, a transport failure.
#             Says nothing about the pin, so it warns only. Note that 2 is
#             non-zero without meaning drift, so callers must test the value
#             rather than mere success (`make` cannot: it collapses any recipe
#             failure to its own exit 2, which is why the make target
#             translates rather than the workflow).
#
# **Only `git` and `curl`** — no `gh`, no token, no API credentials. The repo is
# public, so `git ls-remote` lists tags and resolves a pin to its commit, and
# codeload serves the tarball; both are unauthenticated. This started out on
# `gh api`, which made this script the one task in the project needing a GitHub
# CLI login, and an expired credential then broke vendoring with a 401 that had
# nothing to do with the skills. Neither endpoint here touches the REST API, so
# there is no rate limit to budget for either.
#
# The skill set is *discovered*, not listed here: every top-level directory
# upstream that holds a SKILL.md is vendored, whole tree (several carry
# reference/ subdirectories and companion .md files, not just SKILL.md). So a
# skill added upstream arrives on the next re-vendor without editing this
# script, and one retired upstream is pruned locally.
set -euo pipefail

REPO="mnot/ietf-skill"
REMOTE="https://github.com/$REPO.git"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/tags"

# Exit statuses, shared by the helpers below and by --check.
#   1 -- a definitive answer that something is wrong (drift, or a pin upstream
#        does not have). Fails the build.
#   2 -- we could not find out. Warns; does not fail the build.
readonly WRONG=1
readonly UNKNOWN=2

here="$(cd "$(dirname "$0")/.." && pwd)"
dest="$here/ietf_llm/data/skills"
vendored_md="$dest/VENDORED.md"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# `<sha>\trefs/tags/<name>` for every tag, fetched once per run. Annotated tags
# appear twice: the tag object, and the commit it peels to, as `<name>^{}`.
ls_remote_tags() {
  [[ -s "$work/tags" ]] && { cat "$work/tags"; return 0; }
  if ! git ls-remote --tags "$REMOTE" > "$work/tags" 2> "$work/git.err"; then
    cat "$work/git.err" >&2
    return "$UNKNOWN"
  fi
  cat "$work/tags"
}

# Newest vN.N.N tag upstream (semver-sorted, so remote order doesn't matter).
resolve_latest() {
  ls_remote_tags \
    | sed -n 's|.*refs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$|\1|p' \
    | sort -V | tail -1
}

# The commit a tag names. Prefers the peeled `^{}` entry, which is the commit an
# annotated tag points at; a lightweight tag has only the direct entry. Empty
# output with status 0 means upstream has no such tag — a definitive answer, so
# callers treat that as drift rather than as a failure to ask.
resolve_sha() {  # ref
  local all
  all="$(ls_remote_tags)" || return "$UNKNOWN"
  awk -v r="refs/tags/$1" '$2==r"^{}" {peeled=$1} $2==r {direct=$1}
       END {print (peeled != "" ? peeled : direct)}' <<<"$all"
}

# The tag currently pinned in VENDORED.md.
current_ref() {
  grep -oE 'tag v[0-9]+\.[0-9]+\.[0-9]+' "$vendored_md" | head -1 | awk '{print $2}'
}

# The commit SHA currently pinned in VENDORED.md.
current_sha() {
  grep -oE 'commit [0-9a-f]+' "$vendored_md" | head -1 | awk '{print $2}'
}

# Extract the repo at $1 into $work/src and echo the extracted root. A tarball
# rather than per-file fetches: one request whatever the skill count, and it
# carries subdirectories without walking a tree API.
#
# Fails at the step that actually failed. Letting a 404 fall through to `tar`
# and then to an empty-root guard produced three messages for one fault, the
# last of which ("empty tarball") named a cause that was not the cause.
fetch_tree() {  # ref
  local src="$work/src" code
  mkdir -p "$src"
  # No -f: we want the status code rather than a bare exit 22, so a missing tag
  # (404) can be told apart from a bad minute at GitHub (5xx, timeout).
  if ! code="$(curl -sSL -o "$work/repo.tar.gz" -w '%{http_code}' \
                    "$TARBALL/$1" 2> "$work/curl.err")"; then
    cat "$work/curl.err" >&2
    return "$UNKNOWN"
  fi
  case "$code" in
    2??) : ;;
    404|410) echo "$REPO has no tag $1 (HTTP $code)" >&2; return "$WRONG" ;;
    *) echo "fetching $REPO@$1 returned HTTP $code" >&2; return "$UNKNOWN" ;;
  esac
  if ! tar xzf "$work/repo.tar.gz" -C "$src" 2>/dev/null; then
    echo "$REPO@$1 did not return a tarball" >&2
    return "$UNKNOWN"
  fi
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

# Every directory under $dest, sorted — including any without a SKILL.md, which
# a SKILL.md-keyed listing cannot see while `data/skills/**/*` still ships them.
# An interrupted run between the rm -rf and the cp -R leaves exactly that.
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

# Run a helper that echoes to stdout, storing its output in the variable named
# $1. On failure, report per the helper's status: WRONG is drift and fails the
# build, anything else is "could not ask" and warns. Keeps the --check call
# sites from repeating the same six lines each.
try() {  # var helper args...
  local __var="$1"; shift
  local __out __rc
  set +e
  __out="$("$@")"
  __rc=$?
  set -e
  if [[ $__rc -eq $WRONG ]]; then
    echo "DRIFT: VENDORED.md pins $REPO@$ref, which upstream does not have" >&2
    exit "$WRONG"
  elif [[ $__rc -ne 0 ]]; then
    echo "could not query $REPO (see the error above); vendored skills not checked" >&2
    exit "$UNKNOWN"
  fi
  printf -v "$__var" '%s' "$__out"
}

if [[ "${1:-}" == "--check" ]]; then
  ref="$(current_ref)"
  [[ -n "$ref" ]] || { echo "could not read a pinned tag from VENDORED.md" >&2; exit 1; }

  try root fetch_tree "$ref"
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

  try actual resolve_sha "$ref"
  if [[ -z "$actual" ]]; then
    echo "DRIFT: VENDORED.md pins $REPO@$ref, which upstream does not have" >&2
    exit "$WRONG"
  fi
  if [[ "$(current_sha)" != "$actual" ]]; then
    echo "DRIFT: VENDORED.md pins commit $(current_sha) but $REPO@$ref is $actual" >&2
    status=1
  fi

  [[ $status -eq 0 ]] && echo "vendored skills match $REPO@$ref"
  exit $status
fi

fail() {  # message
  echo "$1" >&2
  echo "nothing vendored." >&2
  exit 1
}

if [[ -n "${1:-}" ]]; then
  ref="$1"
else
  set +e
  ref="$(resolve_latest)"
  set -e
  [[ -n "$ref" ]] || fail "could not list tags for $REPO (see the error above)."
fi

set +e
sha="$(resolve_sha "$ref")"
resolved=$?
set -e
[[ $resolved -eq 0 ]] || fail "could not query $REPO (see the error above)."
[[ -n "$sha" ]] || fail "$REPO has no tag $ref."

set +e
root="$(fetch_tree "$ref")"
fetched=$?
set -e
[[ $fetched -eq 0 ]] || fail "could not fetch $REPO@$ref (see the error above)."

upstream="$(skills_in "$root")"
[[ -n "$upstream" ]] || fail "no skills found in $REPO@$ref."

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
