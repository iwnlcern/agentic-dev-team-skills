#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOT="$(cd "$TOOLS_DIR/.." && pwd)"
HOOK="$SCRIPT_DIR/relay-lint-posttooluse.sh"
BASH_BIN="$(command -v bash)"
tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT
skills_root="$tmp/skills"
mkdir -p "$skills_root/tools"
cp "$TOOLS_DIR/relay-lint.py" "$skills_root/tools/relay-lint.py"
mkdir -p "$tmp/work/.relays/run1" "$tmp/work/src"
# Explicit-file lint is the authoring path: stamped, fresh filenames are the contract; tests author like agents author.
stamp="$(date +%Y%m%d-%H%M%S)"
clean_relay="$tmp/work/.relays/run1/clean-$stamp.md"
fd1_relay="$tmp/work/.relays/run1/dirty-fd1-$stamp.md"
e1_relay="$tmp/work/.relays/run1/dirty-e1-$stamp.md"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E5-clean-tree.md" "$clean_relay"
cp "$TOOLS_DIR/relay-lint-fixtures/fold/FD1-fold-edit-no-foldscope.md" "$fd1_relay"
cp "$TOOLS_DIR/relay-lint-fixtures/content/E1-empty-final-git-status.md" "$e1_relay"
touch "$tmp/work/.relays/run1/.keep" "$tmp/work/.relays/run1/INDEX.md" "$tmp/work/src/note.md"
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
fail=0
PATH_NORMAL="$PATH"
assert_case "a-clean-relay" "$clean_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "b-dirty-fd1" "$fd1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FAILS lint" || fail=1
assert_case "b2-dirty-e1-tripwire" "$e1_relay" "$skills_root" "$tmp/home" "$PATH_NORMAL" 2 "FINAL_GIT_STATUS_SHORT is empty" || fail=1
assert_case "c-non-relay-path" "$tmp/work/src/note.md" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "c2-non-md-relay-root" "$tmp/work/.relays/run1/.keep" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
assert_case "c3-index-md-skipped" "$tmp/work/.relays/run1/INDEX.md" "$skills_root" "$tmp/home" "$PATH_NORMAL" 0 "" || fail=1
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

exit "$fail"
