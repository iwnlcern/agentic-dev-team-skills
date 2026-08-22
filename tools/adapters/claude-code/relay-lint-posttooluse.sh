#!/usr/bin/env bash
# Claude Code PostToolUse hook: lint relay files at write time.
# PostToolUse feedback contract: exit 0 = silent pass; exit 2 = stderr is fed
# back to the agent as feedback (non-blocking). This hook never hard-blocks;
# the protocol's own gates stay authoritative.
set -u
payload="$(cat)"
norm="$(printf '%s' "$payload" | python3 -c '
import json
import os
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR")
    raise SystemExit(0)

tool_input = data.get("tool_input", {}) or {}
file_path = tool_input.get("file_path", "") or ""
command = tool_input.get("command", "") or ""
cwd = data.get("cwd", "") or ""

if file_path:
    print("FILE")
    print(file_path)
    raise SystemExit(0)
if "*** Begin Patch" not in command:
    print("NONE")
    raise SystemExit(0)

print("PATCH")
seg = command[command.index("*** Begin Patch"):]
end = seg.find("*** End Patch")
if end != -1:
    seg = seg[:end + len("*** End Patch")]
print("RELAYREF" if ((".relays/" in seg or "/relays/" in seg) and ".md" in seg) else "NORELAYREF")
for line in command.splitlines():
    stripped = line.strip()
    for marker in ("*** Add File: ", "*** Update File: ", "*** Move to: "):
        if stripped.startswith(marker):
            path = stripped[len(marker):].strip()
            if path and not os.path.isabs(path) and cwd:
                path = os.path.join(cwd, path)
            if path:
                print(path)
')"
kind="${norm%%$'\n'*}"
case "$kind" in
  NONE) exit 0 ;;
  PARSE_ERROR)
    echo "relay-lint-hook: payload unparseable; relay writes may be UNLINTED — lint manually before handoff" >&2
    exit 2
    ;;
  FILE|PATCH) ;;
  *)
    echo "relay-lint-hook: payload normalization failed; relay writes may be UNLINTED — lint manually before handoff" >&2
    exit 2
    ;;
esac

skills_root="${RELAY_LINT_SKILLS_ROOT:-$HOME/.claude/skills}"
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
route_path() {
  mode="file"
  case "$file_path" in
    */.engine/*) return 1 ;;
    */.relays/INDEX.md|*/.relays/*/INDEX.md|*/relays/INDEX.md|*/relays/*/INDEX.md) mode="--index" ;;
    */.relays/*.md|*/relays/*.md) mode="file" ;;
    *) return 1 ;;
  esac
  return 0
}
run_lint() {
  if [ "$mode" = "--index" ]; then
    out="$("$@" --index "$file_path" 2>&1)" || rc=$?
  else
    out="$("$@" "$file_path" ${extra_args+"${extra_args[@]}"} 2>&1)" || rc=$?
  fi
}
lint_current_path() {
  rc=0
  out=""
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
  return 2
fi
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if [ "$rc" -ne 1 ] || printf '%s
' "$out" | grep -Eq 'Traceback|SyntaxError|ModuleNotFoundError|ImportError|usage:'; then
    echo "relay-lint-hook: linter execution failed for $file_path:" >&2
  else
    echo "relay-lint-hook: $file_path FAILS lint:" >&2
  fi
  echo "$out" >&2
  return 2
}

if [ "$kind" = "FILE" ]; then
  file_path="$(printf '%s\n' "$norm" | sed -n '2p')"
  route_path || exit 0
  lint_current_path || exit 2
  exit 0
fi

linted=0
failed=0
envelope_ref="$(printf '%s\n' "$norm" | sed -n '2p')"
while IFS= read -r file_path; do
  [ -n "$file_path" ] || continue
  route_path || continue
  linted=$((linted + 1))
  lint_current_path || failed=1
done < <(printf '%s\n' "$norm" | sed '1,2d')

if [ "$linted" -eq 0 ] && [ "$envelope_ref" = "RELAYREF" ]; then
  echo "relay-lint-hook: patch references relay paths but no lintable target could be extracted (deletes, moves-away, or malformed markers); relay targets may be UNLINTED — lint manually before handoff" >&2
  exit 2
fi
[ "$failed" -eq 0 ] || exit 2
exit 0
