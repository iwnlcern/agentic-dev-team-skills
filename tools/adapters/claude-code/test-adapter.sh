#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOT="$(cd "$TOOLS_DIR/.." && pwd)"
HOOK="$SCRIPT_DIR/relay-lint-posttooluse.sh"
BASH_GUARD="$SCRIPT_DIR/bash-relay-guard.sh"
SETTINGS="$SCRIPT_DIR/settings-snippet.json"
BASH_BIN="$(command -v bash)"
tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT
skills_root="$tmp/skills"
mkdir -p "$skills_root/tools"
cp "$TOOLS_DIR/relay-lint.py" "$skills_root/tools/relay-lint.py"
mkdir -p "$tmp/work/.relays/run1" "$tmp/work/.relays/run2" "$tmp/work/src"
# Explicit-file lint is the authoring path: stamped, fresh filenames are the contract; tests author like agents author.
stamp="$(date +%Y%m%d-%H%M%S)"
clean_relay="$tmp/work/.relays/run1/clean-$stamp.md"
fd1_relay="$tmp/work/.relays/run1/dirty-fd1-$stamp.md"
e1_relay="$tmp/work/.relays/run1/dirty-e1-$stamp.md"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E5-clean-tree.md" "$clean_relay"
cp "$TOOLS_DIR/relay-lint-fixtures/fold/FD1-fold-edit-no-foldscope.md" "$fd1_relay"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E1-empty-final-git-status.md" "$e1_relay"
index_relay="$tmp/work/.relays/run1/INDEX.md"
raw_split_index="$tmp/work/.relays/run2/INDEX.md"
touch "$tmp/work/.relays/run1/.keep" "$index_relay" "$tmp/work/src/note.md"
run_hook() {
  local file="$1" skills="$2" home_dir="$3" path_value="$4"
  printf '{"tool_input":{"file_path":"%s"}}' "$file" | RELAY_LINT_SKILLS_ROOT="$skills" HOME="$home_dir" PATH="$path_value" "$BASH_BIN" "$HOOK" 2>"$tmp/stderr"
}
assert_case() {
  local name="$1" file="$2" skills="$3" home_dir="$4" path_value="$5" expected="$6" want="$7"
  run_hook "$file" "$skills" "$home_dir" "$path_value"
  local rc=$?
  if [ "$rc" -ne "$expected" ]; then
    echo "FAIL $name: expected exit $expected got $rc" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if [ -n "$want" ] && ! grep -Fq "$want" "$tmp/stderr"; then
    echo "FAIL $name: expected stderr to contain: $want" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if [ -z "$want" ] && [ -s "$tmp/stderr" ]; then
    echo "FAIL $name: expected silent stderr" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  echo "PASS $name"
}
run_bash_guard() {
  local command="$1" background="$2" event="$3" skills="$4" home_dir="$5" path_value="$6"
  python3 -c 'import json,sys; print(json.dumps({"hook_event_name":sys.argv[1],"tool_input":{"command":sys.argv[2],"run_in_background":sys.argv[3] == "true"}}))' \
    "$event" "$command" "$background" \
    | RELAY_LINT_SKILLS_ROOT="$skills" HOME="$home_dir" PATH="$path_value" "$BASH_BIN" "$BASH_GUARD" 2>"$tmp/stderr"
}
assert_guard_case() {
  local name="$1" command="$2" background="$3" event="$4" expected="$5" want="$6"
  shift 6
  run_bash_guard "$command" "$background" "$event" "$skills_root" "$tmp/home" "$PATH_NORMAL"
  local rc=$?
  if [ "$rc" -ne "$expected" ]; then
    echo "FAIL $name: expected exit $expected got $rc" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if [ -n "$want" ] && ! grep -Fq "$want" "$tmp/stderr"; then
    echo "FAIL $name: expected stderr to contain: $want" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if [ -z "$want" ] && [ -s "$tmp/stderr" ]; then
    echo "FAIL $name: expected silent stderr" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  local forbidden
  for forbidden in "$@"; do
    if grep -Fq "$forbidden" "$tmp/stderr"; then
      echo "FAIL $name: stderr must not contain: $forbidden" >&2
      cat "$tmp/stderr" >&2
      return 1
    fi
  done
  echo "PASS $name"
}
fail=0
PATH_NORMAL="$PATH"
assert_case "a-clean-relay" "$clean_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "b-dirty-fd1" "$fd1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FAILS lint" || fail=1
assert_case "b2-dirty-e1-tripwire" "$e1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FINAL_GIT_STATUS_SHORT is empty" || fail=1
assert_case "c-non-relay-path" "$tmp/work/src/note.md" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "c2-non-md-relay-root" "$tmp/work/.relays/run1/.keep" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "c3-empty-index-md" "$index_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "no index rows found" || fail=1
printf '%s\n' \
  '| time | phase | role | dispatch | parent | from | to | cc | status | file |' \
  '|---|---|---|---|---|---|---|---|---|---|' \
  '| 20260601-120000 | AUDIT | Planner | d-e5 | — | pair-1.planner | orchestrator | — | returned | AUDIT-planner-20260601-120000.md |' \
  > "$index_relay"
assert_case "i1-valid-index-md" "$index_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
printf '%s\n' '| 20260601-110000 | AUDIT | Planner | d-e5 | — | pair-1.planner | orchestrator | — | returned | AUDIT-planner-20260601-110000.md |' >> "$index_relay"
assert_case "i2-decreasing-index-row" "$index_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "precedes the previous row" || fail=1
printf '%s\n' \
  '| time | phase | role | dispatch | parent | from | to | cc | status | file |' \
  '|---|---|---|---|---|---|---|---|---|---|' \
  '| 20260601-120000 | AUDIT | Planner | d-e5 | — | pair-1.planner | orchestrator | — | returned | AUDIT-planner-20260601-120000.md |' \
  '| 20260601-130000 | AUDIT | Planner | d-e5 | — | pair-1.planner | orchestrator | — | returned | raw | split |' \
  > "$raw_split_index"
assert_case "i3-raw-split-index-row" "$raw_split_index" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "header declares" || fail=1
mkdir -p "$tmp/bin" "$tmp/empty-skills" "$tmp/empty-home"
ln -s "$(command -v python3)" "$tmp/bin/python3"
if PATH="$tmp/bin" command -v relay-lint >/dev/null 2>&1; then
  echo "FAIL d-no-linter: sanitized PATH unexpectedly has relay-lint" >&2
  fail=1
else
  assert_case "d-no-linter" "$clean_relay" "$tmp/empty-skills" "$tmp/empty-home" "$tmp/bin" 2 "UNLINTED" || fail=1
fi

# Broken linter should be attributed to linter execution, not relay lint failure.
broken_skills="$tmp/broken-skills"
mkdir -p "$broken_skills/tools"
printf 'definitely not python\n' > "$broken_skills/tools/relay-lint.py"
assert_case "e-broken-linter" "$clean_relay" "$broken_skills" "$tmp/home" "$PATH_NORMAL" 2 "linter execution failed" || fail=1

g1_relay="$tmp/work/.relays/run1/g1-$stamp.md"
printf 'x\n' > "$g1_relay"
assert_guard_case "g1-literal-relay-redirect" "printf x > $g1_relay" false PostToolUse 2 "FAILS lint" || fail=1

g2_target="$tmp/work/src/x.md"
printf 'x\n' > "$g2_target"
assert_guard_case "g2-non-relay-redirect" "printf x > $g2_target" false PostToolUse 0 "" || fail=1

g3_index="$tmp/work/.relays/run1/INDEX.md"
printf '%s\n' \
  '| time | phase | role | dispatch | parent | from | to | cc | status | file |' \
  '|---|---|---|---|---|---|---|---|---|---|' \
  '| 20260601-130000 | AUDIT | Planner | d-g3 | — | pair-1.planner | orchestrator | — | returned | raw | split |' \
  > "$g3_index"
assert_guard_case "g3-index-heredoc-routes-index-mode" "cat <<'EOF' > $g3_index" false PostToolUse 2 "header declares" || fail=1

printf 'x\n' > "$tmp/work/a.md"
assert_guard_case "g4-mv-to-relay-directory-is-generic" "mv $tmp/work/a.md $tmp/work/.relays/run1/" false PostToolUse 2 "could not be linted" || fail=1

g5_relay="$tmp/work/.relays/run1/g5-$stamp.md"
printf 'x\n' > "$g5_relay"
assert_guard_case "g5-interpreter-one-liner-is-residual" "python3 -c \"open('$g5_relay','w')\"" false PostToolUse 0 "" || fail=1

g6_relay="$tmp/work/.relays/run1/g6-$stamp.md"
printf 'x\n' > "$g6_relay"
assert_guard_case "g6-background-write-is-generic" "printf x > $g6_relay" true PostToolUse 2 "backgrounded Bash command" "FAILS lint" || fail=1

g7_relay="$tmp/work/.relays/run1/g7-$stamp.md"
printf 'x\n' > "$g7_relay"
assert_guard_case "g7-write-then-false-failure-event" "printf x > $g7_relay; false" false PostToolUseFailure 2 "FAILS lint" || fail=1

g8_one="$tmp/work/.relays/run1/one-$stamp.md"
g8_two="$tmp/work/.relays/run1/two-$stamp.md"
printf 'x\n' > "$g8_one"
printf 'x\n' > "$g8_two"
printf 'x\n' > "$tmp/work/b.md"
assert_guard_case "g8-multiple-relay-targets-are-generic" "cp $tmp/work/a.md $g8_one; cp $tmp/work/b.md $g8_two" false PostToolUse 2 "could not be linted" "$(basename "$g8_one")" "$(basename "$g8_two")" "FAILS lint" || fail=1

g9_relay="$tmp/work/.relays/run1/NOTINDEX-$stamp.md"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E5-clean-tree.md" "$g9_relay"
assert_guard_case "g9-notindex-routes-explicit-file-mode" "printf x > $g9_relay" false PostToolUse 0 "" || fail=1

# This proves SHIPPED CONFIGURATION, not live host installation.
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); expected="bash \"$HOME/.claude/skills/tools/adapters/claude-code/bash-relay-guard.sh\""; events=("PostToolUse","PostToolUseFailure"); assert all(any(e.get("matcher") == "Bash" and any(h.get("type") == "command" and h.get("command") == expected for h in e.get("hooks", [])) for e in d.get("hooks", {}).get(event, [])) for event in events)' "$SETTINGS"; then
  echo "PASS s1-shipped-bash-registration-both-events"
else
  echo "FAIL s1-shipped-bash-registration-both-events: expected Bash guard under PostToolUse and PostToolUseFailure" >&2
  fail=1
fi

exit "$fail"
