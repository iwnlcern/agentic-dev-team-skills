#!/usr/bin/env bash
# Scope proof for PL-v295-b2 (plan-3 P8): provenance-preserving classification with checked collection.
# usage: scope_proof.sh <base-sha> [expected-staged-path ...]
set -euo pipefail
BASE="${1:?base sha}"; shift || true
allowed='^(tools/adapters/|tools/generate-plugins\.py$|tools/check-generate-plugins\.py$|shared/protocol\.md$|shared/harness-codex\.md$|skills/|README\.md$|CHANGELOG\.md$|plugins/)'
known_untracked='^docs/(releases|reports|sprints)/'
tracked="$(git diff --name-only "$BASE")"
staged="$(git diff --name-only --cached)"
untracked="$(git ls-files --others --exclude-standard)"
bad=0
classify() {
  local kind="$1" path="$2"
  [ -z "$path" ] && return 0
  if printf '%s' "$path" | grep -qE "$allowed"; then return 0; fi
  if [ "$kind" = untracked ] && printf '%s' "$path" | grep -qE "$known_untracked"; then return 0; fi
  echo "SCOPE DEVIATION ($kind): $path"; bad=1
}
while IFS= read -r p; do classify tracked "$p"; done <<<"$tracked"
while IFS= read -r p; do classify staged "$p"; done <<<"$staged"
while IFS= read -r p; do classify untracked "$p"; done <<<"$untracked"
if [ "$#" -gt 0 ]; then
  actual="$(printf '%s\n' "$staged" | sed '/^$/d' | sort)"
  want="$(printf '%s\n' "$@" | sort)"
  if [ "$actual" != "$want" ]; then
    echo "STAGED SET MISMATCH"; echo "expected:"; echo "$want"; echo "actual:"; echo "$actual"; bad=1
  fi
fi
if [ "$bad" -eq 0 ]; then echo "scope ok"; else exit 1; fi
