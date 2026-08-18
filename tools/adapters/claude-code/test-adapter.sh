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
assert_case_engine_json() {
  local name="$1" file="$2" skills="$3" home_dir="$4" path_value="$5" expected="$6"
  run_hook "$file" "$skills" "$home_dir" "$path_value"
  local rc=$?
  if [ "$rc" -ne "$expected" ]; then
    echo "FAIL $name: expected exit $expected got $rc" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if ! grep -q '"errors"' "$tmp/stderr"; then
    echo "FAIL $name: expected engine JSON errors body" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  echo "PASS $name"
}
stage_engine_root() {
  local root="$1" generation="$2"
  rm -rf "$root/tools"
  mkdir -p "$root/tools"
  cp "$TOOLS_DIR/relay" "$root/tools/relay"
  cp -R "$TOOLS_DIR/relay_engine" "$root/tools/relay_engine"
  # The trace is test-fixture instrumentation in a real roster member: the
  # hook still executes the bundled CLI and rules, while the selected cache
  # prefix becomes observable without replacing it with a mock.
  printf '%s\n' \
    'import os as _e3_os' \
    'if _e3_os.environ.get("ADT_E3_ENGINE_TRACE"):' \
    '    with open(_e3_os.environ["ADT_E3_ENGINE_TRACE"], "a", encoding="utf-8") as _e3_trace:' \
    "        _e3_trace.write(\"$generation\\n\")" \
    >> "$root/tools/relay_engine/__init__.py"
}
expected_engine_fingerprint() {
  local tools_dir="$1"
  PYTHONPATH="$TOOLS_DIR" python3 -c \
    'from pathlib import Path; import sys; from relay_engine.version import fingerprint; print(fingerprint(Path(sys.argv[1])))' \
    "$tools_dir"
}
assert_relay_version() {
  local name="$1" relay="$2" install="$3" kit="$4" fingerprint="$5"
  local output rc
  output="$("$relay" version 2>"$tmp/stderr")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL $name: expected relay version exit 0, got $rc" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if ! python3 -c '
import json
import os
import sys
actual = json.loads(sys.stdin.read())
expected = {"fingerprint": sys.argv[3], "install": os.path.realpath(sys.argv[1]), "kit": sys.argv[2]}
raise SystemExit(0 if actual == expected else 1)
' "$install" "$kit" "$fingerprint" <<<"$output"; then
    echo "FAIL $name: expected exact kit/fingerprint/install triad" >&2
    echo "expected install=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$install") kit=$kit fingerprint=$fingerprint" >&2
    echo "actual $output" >&2
    return 1
  fi
  echo "PASS $name"
}
assert_case_engine_json_origin() {
  local name="$1" file="$2" skills="$3" home_dir="$4" path_value="$5" expected="$6" origin="$7"
  local trace="$tmp/e3-engine-trace"
  : > "$trace"
  printf '{"tool_input":{"file_path":"%s"}}' "$file" | ADT_E3_ENGINE_TRACE="$trace" RELAY_LINT_SKILLS_ROOT="$skills" HOME="$home_dir" PATH="$path_value" "$BASH_BIN" "$HOOK" 2>"$tmp/stderr"
  local rc=$?
  if [ "$rc" -ne "$expected" ]; then
    echo "FAIL $name: expected exit $expected got $rc" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if ! grep -q '"errors"' "$tmp/stderr"; then
    echo "FAIL $name: expected engine JSON errors body" >&2
    cat "$tmp/stderr" >&2
    return 1
  fi
  if [ "$(cat "$trace")" != "$origin" ]; then
    echo "FAIL $name: expected only configured prefix $origin, got $(tr '\n' ' ' < "$trace")" >&2
    return 1
  fi
  echo "PASS $name"
}
stage_unsearched_prefix_decoys() {
  local home_dir="$1"
  mkdir -p "$home_dir/.agents/skills/tools" "$home_dir/.codex/skills/tools"
  printf '%s\n' '#!/bin/sh' 'echo DECOY-UNSEARCHED-PREFIX >&2' 'exit 1' \
    > "$home_dir/.agents/skills/tools/relay-lint.py"
  cp "$home_dir/.agents/skills/tools/relay-lint.py" "$home_dir/.codex/skills/tools/relay-lint.py"
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

# v1-v5 name the visible-root recognition boundary. Each shape is exercised
# through both adapter entry points so their routing cannot drift apart.
visible_relay="$tmp/work/master/relays/run1/v1-$stamp.md"
visible_index_root="$tmp/work/master/relays/INDEX.md"
visible_index_nested="$tmp/work/master/relays/run1/INDEX.md"
near_miss="$tmp/work/docs/relays-notes.md"
hidden_relay="$tmp/work/.relays/run1/v4-$stamp.md"
hidden_index="$tmp/work/.relays/run-v5/INDEX.md"
mkdir -p "$tmp/work/master/relays/run1" "$tmp/work/.relays/run-v5"

printf 'x\n' > "$visible_relay"
assert_case "v1-write-visible-relay-root" "$visible_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FAILS lint" || fail=1
printf 'x\n' > "$visible_relay"
assert_guard_case "v1-bash-visible-relay-root" "printf x > $visible_relay" false PostToolUse 2 "FAILS lint" || fail=1

: > "$visible_index_root"
assert_case "v2-write-visible-index-direct-root" "$visible_index_root" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "no index rows found" || fail=1
: > "$visible_index_root"
assert_guard_case "v2-bash-visible-index-direct-root" "printf x > $visible_index_root" false PostToolUse 2 "no index rows found" || fail=1
: > "$visible_index_nested"
assert_case "v2-write-visible-index-nested" "$visible_index_nested" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "no index rows found" || fail=1
: > "$visible_index_nested"
assert_guard_case "v2-bash-visible-index-nested" "printf x > $visible_index_nested" false PostToolUse 2 "no index rows found" || fail=1

assert_case "v3-write-relays-near-miss" "$near_miss" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_guard_case "v3-bash-relays-near-miss" "printf x > $near_miss" false PostToolUse 0 "" || fail=1

printf 'x\n' > "$hidden_relay"
assert_case "v4-write-hidden-relay-root" "$hidden_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FAILS lint" || fail=1
printf 'x\n' > "$hidden_relay"
assert_guard_case "v4-bash-hidden-relay-root" "printf x > $hidden_relay" false PostToolUse 2 "FAILS lint" || fail=1

: > "$hidden_index"
assert_case "v5-write-hidden-index" "$hidden_index" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "no index rows found" || fail=1
: > "$hidden_index"
assert_guard_case "v5-bash-hidden-index" "printf x > $hidden_index" false PostToolUse 2 "no index rows found" || fail=1

assert_case "a-clean-relay" "$clean_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "b-dirty-fd1" "$fd1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FAILS lint" || fail=1
assert_case "b2-dirty-e1-tripwire" "$e1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FINAL_GIT_STATUS_SHORT is empty" || fail=1
assert_case "c-non-relay-path" "$tmp/work/src/note.md" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "c2-non-md-relay-root" "$tmp/work/.relays/run1/.keep" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1

# Engine-capable installs must choose the structured engine command ahead of
# the standalone compatibility linter; the fallback root intentionally lacks
# both engine artifacts.
engine_skills="$tmp/engine-skills"
mkdir -p "$engine_skills/tools"
cp "$TOOLS_DIR/relay" "$engine_skills/tools/relay"
cp -R "$TOOLS_DIR/relay_engine" "$engine_skills/tools/relay_engine"
cp "$TOOLS_DIR/relay-lint.py" "$engine_skills/tools/relay-lint.py"
engine_clean="$tmp/work/.relays/run1/engine-clean-$stamp.md"
engine_dirty="$tmp/work/.relays/run1/engine-dirty-$stamp.md"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E5-clean-tree.md" "$engine_clean"
cp "$TOOLS_DIR/relay-lint-fixtures/fold/FD1-fold-edit-no-foldscope.md" "$engine_dirty"
assert_case_engine_json "engine-preferred-dirty" "$engine_dirty" "$engine_skills" "$tmp/home" "$PATH_NORMAL" 2 || fail=1
assert_case "engine-preferred-clean" "$engine_clean" "$engine_skills" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "engine-absent-fallback" "$fd1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FAILS lint" || fail=1
engine_draft="$tmp/work/.relays/x/.engine/drafts/seat/DIRTY.md"
mkdir -p "$(dirname "$engine_draft")"
cp "$TOOLS_DIR/relay-lint-fixtures/fold/FD1-fold-edit-no-foldscope.md" "$engine_draft"
assert_case "engine-drafts-exempt" "$engine_draft" "$engine_skills" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1

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

# f0-f4 pin the complete four-step resolution order:
# engine-capable configured root -> standalone configured root -> ~/.agents/skills
# -> deprecated ~/.codex/skills. PATH is deliberately never a resolver.
# f0: a live configured root beats a conflicting .agents linter.
# f1: .agents beats the deprecated root when the configured root is absent.
# f2: the deprecated root still resolves alone.
# f3: the deprecated root beats a conflicting PATH linter.
# f4: no filesystem resolver emits UNLINTED even when PATH carries a decoy.
fallback_home="$tmp/home-fallback"
mkdir -p "$fallback_home/.agents/skills/tools" "$fallback_home/.codex/skills/tools"
cp "$TOOLS_DIR/relay-lint.py" "$fallback_home/.agents/skills/tools/relay-lint.py"
printf '%s\n' '#!/usr/bin/env python3' 'import sys' 'print("DECOY-CODEX-LINTER", file=sys.stderr)' 'sys.exit(1)' \
  > "$fallback_home/.codex/skills/tools/relay-lint.py"
absent_root="$tmp/no-such-skills-root"
fallback_relay="$tmp/work/.relays/run1/fallback-$stamp.md"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E5-clean-tree.md" "$fallback_relay"

confroot_home="$tmp/home-confroot"
mkdir -p "$confroot_home/.agents/skills/tools"
printf '%s\n' '#!/usr/bin/env python3' 'import sys' 'print("DECOY-AGENTS-LINTER", file=sys.stderr)' 'sys.exit(1)' \
  > "$confroot_home/.agents/skills/tools/relay-lint.py"

# PATH is decoy-armed for f0-f3: an ambient relay-lint on the host must never
# be reachable as a silent lower fallthrough, or precedence passes stop being
# exclusive to the claimed winner (R8).
decoy_bin="$tmp/decoy-bin"
mkdir -p "$decoy_bin"
printf '%s\n' '#!/bin/sh' 'echo "DECOY-PATH-LINTER" >&2' 'exit 1' > "$decoy_bin/relay-lint"
chmod +x "$decoy_bin/relay-lint"
PATH_WITH_DECOY="$decoy_bin:$PATH_NORMAL"

assert_case "f0-write-confroot-beats-agents" "$fallback_relay" "$skills_root" "$confroot_home" "$PATH_WITH_DECOY" 0 "" || fail=1
run_bash_guard "printf x >> $fallback_relay" false PostToolUse "$skills_root" "$confroot_home" "$PATH_WITH_DECOY"
rc=$?
if [ "$rc" -ne 0 ] || [ -s "$tmp/stderr" ]; then
  echo "FAIL f0-bash-confroot-beats-agents: expected silent exit 0, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS f0-bash-confroot-beats-agents"
fi

assert_case "f1-write-agents-beats-deprecated" "$fallback_relay" "$absent_root" "$fallback_home" "$PATH_WITH_DECOY" 0 "" || fail=1
run_bash_guard "printf x >> $fallback_relay" false PostToolUse "$absent_root" "$fallback_home" "$PATH_WITH_DECOY"
rc=$?
if [ "$rc" -ne 0 ] || [ -s "$tmp/stderr" ]; then
  echo "FAIL f1-bash-agents-beats-deprecated: expected silent exit 0, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS f1-bash-agents-beats-deprecated"
fi

rm "$fallback_home/.agents/skills/tools/relay-lint.py"
assert_case "f2-write-deprecated-still-resolves" "$fallback_relay" "$absent_root" "$fallback_home" "$PATH_WITH_DECOY" 2 "DECOY-CODEX-LINTER" || fail=1
run_bash_guard "printf x >> $fallback_relay" false PostToolUse "$absent_root" "$fallback_home" "$PATH_WITH_DECOY"
rc=$?
if [ "$rc" -ne 2 ] || ! grep -Fq "DECOY-CODEX-LINTER" "$tmp/stderr"; then
  echo "FAIL f2-bash-deprecated-still-resolves: expected exit 2 with decoy marker, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS f2-bash-deprecated-still-resolves"
fi

pathbeat_home="$tmp/home-pathbeat"
mkdir -p "$pathbeat_home/.codex/skills/tools"
cp "$TOOLS_DIR/relay-lint.py" "$pathbeat_home/.codex/skills/tools/relay-lint.py"

assert_case "f3-write-deprecated-beats-path" "$fallback_relay" "$absent_root" "$pathbeat_home" "$PATH_WITH_DECOY" 0 "" || fail=1
run_bash_guard "printf x >> $fallback_relay" false PostToolUse "$absent_root" "$pathbeat_home" "$PATH_WITH_DECOY"
rc=$?
if [ "$rc" -ne 0 ] || [ -s "$tmp/stderr" ]; then
  echo "FAIL f3-bash-deprecated-beats-path: expected silent exit 0, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS f3-bash-deprecated-beats-path"
fi

empty_home="$tmp/home-empty"
mkdir -p "$empty_home"
real_bin="$tmp/real-bin"
mkdir -p "$real_bin"
printf '%s\n' '#!/bin/sh' 'echo "DECOY-PATH-LINTER" >&2' 'exit 1' > "$real_bin/relay-lint"
chmod +x "$real_bin/relay-lint"
PATH_WITH_FAKE="$real_bin:$PATH_NORMAL"

assert_case "f4-write-path-only-is-unlinted" "$fallback_relay" "$absent_root" "$empty_home" "$PATH_WITH_FAKE" 2 "UNLINTED" || fail=1
run_bash_guard "printf x >> $fallback_relay" false PostToolUse "$absent_root" "$empty_home" "$PATH_WITH_FAKE"
rc=$?
if [ "$rc" -ne 2 ] || ! grep -Fq "UNLINTED" "$tmp/stderr" || grep -Fq "DECOY-PATH-LINTER" "$tmp/stderr"; then
  echo "FAIL f4-bash-path-only-is-unlinted: expected UNLINTED exit 2 without PATH execution, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS f4-bash-path-only-is-unlinted"
fi

# l1 pins D1's byte-identity mandate: the resolution ladder must stay
# byte-identical across both hook entry points (variants Task 4 Step 2 extraction).
awk '/^if \[ -f "\$skills_root\/tools\/relay" \] && \[ -d "\$skills_root\/tools\/relay_engine" \]; then$/,/^else$/' \
  "$SCRIPT_DIR/relay-lint-posttooluse.sh" > "$tmp/ladder-hook"
awk '/^if \[ -f "\$skills_root\/tools\/relay" \] && \[ -d "\$skills_root\/tools\/relay_engine" \]; then$/,/^else$/' \
  "$SCRIPT_DIR/bash-relay-guard.sh" > "$tmp/ladder-guard"
if [ ! -s "$tmp/ladder-hook" ]; then
  echo "FAIL l1-ladder-byte-identity: empty extraction — awk range did not match" >&2; fail=1
elif ! cmp -s "$tmp/ladder-hook" "$tmp/ladder-guard"; then
  echo "FAIL l1-ladder-byte-identity: resolution ladders differ between hook and guard" >&2
  diff "$tmp/ladder-hook" "$tmp/ladder-guard" >&2; fail=1
else
  echo "PASS l1-ladder-byte-identity"
fi

# k1-k3 pin the H30 edit-tolerant freshness knobs on the write-time hook: the
# strict default still rejects an old authoring stamp, and each supported knob
# lets an edit of an older relay pass without weakening anything else.
knob_relay="$tmp/work/.relays/run1/clean-20200101-000000.md"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E5-clean-tree.md" "$knob_relay"
run_hook_env() {
  local file="$1" skills="$2"; shift 2
  printf '{"tool_input":{"file_path":"%s"}}' "$file" | env "$@" RELAY_LINT_SKILLS_ROOT="$skills" HOME="$tmp/home" PATH="$PATH_NORMAL" "$BASH_BIN" "$HOOK" 2>"$tmp/stderr"
}
run_hook_env "$knob_relay" "$skills_root"
rc=$?
if [ "$rc" -ne 2 ] || ! grep -Fq "in the past" "$tmp/stderr"; then
  echo "FAIL k1-strict-default-rejects-old-stamp: expected exit 2 with drift error, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS k1-strict-default-rejects-old-stamp"
fi
run_hook_env "$knob_relay" "$skills_root" RELAY_LINT_NO_FRESHNESS=1
rc=$?
if [ "$rc" -ne 0 ] || [ -s "$tmp/stderr" ]; then
  echo "FAIL k2-no-freshness-knob-passes-old-stamp: expected silent exit 0, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS k2-no-freshness-knob-passes-old-stamp"
fi
run_hook_env "$knob_relay" "$skills_root" RELAY_LINT_MAX_DRIFT_MINUTES=99999999
rc=$?
if [ "$rc" -ne 0 ] || [ -s "$tmp/stderr" ]; then
  echo "FAIL k3-max-drift-knob-passes-old-stamp: expected silent exit 0, got $rc" >&2; cat "$tmp/stderr" >&2; fail=1
else
  echo "PASS k3-max-drift-knob-passes-old-stamp"
fi

# E3 activation proves the documented shared-root refresh route with the
# real bundled engine.  The break this catches is a hook that falls back to
# standalone lint or a relay executable that reports the wrong install bytes.
e3_shared_root="$tmp/e3-shared-root"
stage_engine_root "$e3_shared_root" A
e3_shared_a_fingerprint="$(expected_engine_fingerprint "$e3_shared_root/tools")"
assert_case_engine_json_origin "e3-shared-vA-hook-engine-json" "$engine_dirty" "$e3_shared_root" "$tmp/home" "$PATH_NORMAL" 2 A || fail=1
assert_relay_version "e3-shared-vA-version-triad" "$e3_shared_root/tools/relay" "$e3_shared_root/tools" "2.9.0" "$e3_shared_a_fingerprint" || fail=1
stage_engine_root "$e3_shared_root" B
e3_shared_b_fingerprint="$(expected_engine_fingerprint "$e3_shared_root/tools")"
if [ "$e3_shared_a_fingerprint" = "$e3_shared_b_fingerprint" ]; then
  echo "FAIL e3-shared-refresh-distinct-fingerprints: vA and vB fingerprints match" >&2
  fail=1
else
  echo "PASS e3-shared-refresh-distinct-fingerprints"
fi
assert_case_engine_json_origin "e3-shared-vB-hook-engine-json" "$engine_dirty" "$e3_shared_root" "$tmp/home" "$PATH_NORMAL" 2 B || fail=1
assert_relay_version "e3-shared-vB-version-triad" "$e3_shared_root/tools/relay" "$e3_shared_root/tools" "2.9.0" "$e3_shared_b_fingerprint" || fail=1

# E3 activation also proves that a version-qualified plugin cache remains at
# the configured root after an update and changes only after an explicit
# repoint.  Decoy fallback roots make any search beyond the configured prefix
# observable instead of silently succeeding.
e3_plugin_cache="$tmp/e3-plugin-cache"
e3_plugin_old="$e3_plugin_cache/adt-master/2.9.0"
e3_plugin_new="$e3_plugin_cache/adt-master/2.9.1"
e3_plugin_home="$tmp/e3-plugin-home"
stage_engine_root "$e3_plugin_old" A
stage_engine_root "$e3_plugin_new" B
e3_plugin_old_fingerprint="$(expected_engine_fingerprint "$e3_plugin_old/tools")"
e3_plugin_new_fingerprint="$(expected_engine_fingerprint "$e3_plugin_new/tools")"
if [ "$e3_plugin_old_fingerprint" = "$e3_plugin_new_fingerprint" ]; then
  echo "FAIL e3-plugin-staged-distinct-fingerprints: 2.9.0 and 2.9.1 fingerprints match" >&2
  fail=1
else
  echo "PASS e3-plugin-staged-distinct-fingerprints"
fi
stage_unsearched_prefix_decoys "$e3_plugin_home"
assert_case_engine_json_origin "e3-plugin-update-keeps-old-hook-engine-json" "$engine_dirty" "$e3_plugin_old" "$e3_plugin_home" "$PATH_WITH_DECOY" 2 A || fail=1
assert_relay_version "e3-plugin-update-keeps-old-version-triad" "$e3_plugin_old/tools/relay" "$e3_plugin_old/tools" "2.9.0" "$e3_plugin_old_fingerprint" || fail=1
assert_case_engine_json_origin "e3-plugin-repoint-new-hook-engine-json" "$engine_dirty" "$e3_plugin_new" "$e3_plugin_home" "$PATH_WITH_DECOY" 2 B || fail=1
assert_relay_version "e3-plugin-repoint-new-version-triad" "$e3_plugin_new/tools/relay" "$e3_plugin_new/tools" "2.9.0" "$e3_plugin_new_fingerprint" || fail=1

exit "$fail"
