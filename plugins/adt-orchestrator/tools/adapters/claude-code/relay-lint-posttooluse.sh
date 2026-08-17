#!/usr/bin/env bash
# Claude Code PostToolUse hook: lint relay files at write time.
# PostToolUse feedback contract: exit 0 = silent pass; exit 2 = stderr is fed
# back to the agent as feedback (non-blocking). This hook never hard-blocks;
# the protocol's own gates stay authoritative.
set -u
file_path="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path", ""))')"
mode="file"
case "$file_path" in
  */.engine/*) exit 0 ;;
  */.relays/INDEX.md|*/.relays/*/INDEX.md|*/relays/INDEX.md|*/relays/*/INDEX.md) mode="--index" ;;
  */.relays/*.md|*/relays/*.md) mode="file" ;;
  *) exit 0 ;;
esac
skills_root="${RELAY_LINT_SKILLS_ROOT:-$HOME/.claude/skills}"
rc=0
# Edit-tolerant freshness posture (H30): RELAY_LINT_MAX_DRIFT_MINUTES widens
# the authoring-drift tolerance; RELAY_LINT_NO_FRESHNESS=1 skips it entirely
# (editing an older relay is not authoring a new one). Defaults unchanged.
extra_args=()
if [ -n "${RELAY_LINT_MAX_DRIFT_MINUTES:-}" ]; then
  extra_args+=(--max-drift-minutes "$RELAY_LINT_MAX_DRIFT_MINUTES")
fi
if [ "${RELAY_LINT_NO_FRESHNESS:-0}" = "1" ]; then
  extra_args+=(--no-freshness)
fi
run_lint() {
  if [ "$mode" = "--index" ]; then
    out="$("$@" --index "$file_path" 2>&1)" || rc=$?
  else
    out="$("$@" "$file_path" ${extra_args+"${extra_args[@]}"} 2>&1)" || rc=$?
  fi
}
if [ -f "$skills_root/tools/relay" ] && [ -d "$skills_root/tools/relay_engine" ]; then
  run_lint python3 "$skills_root/tools/relay" lint
elif [ -f "$skills_root/tools/relay-lint.py" ]; then
  run_lint python3 "$skills_root/tools/relay-lint.py"
elif [ -f "$HOME/.agents/skills/tools/relay-lint.py" ]; then
  run_lint python3 "$HOME/.agents/skills/tools/relay-lint.py"
elif [ -f "$HOME/.codex/skills/tools/relay-lint.py" ]; then
  # deprecated Codex root, retained for back-compat
  run_lint python3 "$HOME/.codex/skills/tools/relay-lint.py"
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
