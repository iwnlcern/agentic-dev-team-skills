#!/usr/bin/env bash
# Claude Code PostToolUse hook: lint relay files at write time.
# PostToolUse feedback contract: exit 0 = silent pass; exit 2 = stderr is fed
# back to the agent as feedback (non-blocking). This hook never hard-blocks;
# the protocol's own gates stay authoritative.
set -u
file_path="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path", ""))')"
mode="file"
case "$file_path" in
  */.relays/INDEX.md|*/.relays/*/INDEX.md|*/relays/INDEX.md|*/relays/*/INDEX.md) mode="--index" ;;
  */.relays/*.md|*/relays/*.md) mode="file" ;;
  *) exit 0 ;;
esac
skills_root="${RELAY_LINT_SKILLS_ROOT:-$HOME/.claude/skills}"
rc=0
run_lint() {
  if [ "$mode" = "--index" ]; then
    out="$("$@" --index "$file_path" 2>&1)" || rc=$?
  else
    out="$("$@" "$file_path" 2>&1)" || rc=$?
  fi
}
if [ -f "$skills_root/tools/relay-lint.py" ]; then
  run_lint python3 "$skills_root/tools/relay-lint.py"
elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
elif command -v relay-lint >/dev/null 2>&1; then
  run_lint relay-lint
else
  echo "relay-lint-hook: linter not found at any installed location; relay $file_path UNLINTED" >&2
  exit 2
fi
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -ne 1 ] || printf '%s
' "$out" | grep -Eq 'Traceback|SyntaxError|ModuleNotFoundError|ImportError|usage:'; then
    echo "relay-lint-hook: linter execution failed for $file_path:" >&2
  else
    echo "relay-lint-hook: $file_path FAILS lint:" >&2
  fi
  echo "$out" >&2
  exit 2
fi
exit 0
