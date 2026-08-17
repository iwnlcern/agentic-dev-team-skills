#!/usr/bin/env bash
# Advisory guard for Bash-channel relay writes. Non-blocking: exit 0 silent,
# exit 2 stderr advisory.
set -u
payload="$(cat)"
command="$(printf '%s' "$payload" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("command",""))')"
bg="$(printf '%s' "$payload" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(str(d.get("tool_input",{}).get("run_in_background",False)).lower())')"
case "$command" in
  *".relays/"*|*"/relays/"*) ;;
  *) exit 0 ;;
esac
case "$command" in
  *">"*|*"tee "*|*"cp "*|*"mv "*) ;;
  *) exit 0 ;;
esac
targets="$(printf '%s' "$command" | grep -oE '[^ >]*(\.relays|/relays)/[^ ;|&"'"'"']*\.md' | sort -u)"
target_count="$(printf '%s' "$targets" | grep -c . || true)"
target=""
[ "$target_count" = "1" ] && target="$targets"
case "$target" in
  */.engine/*) exit 0 ;;
esac
if [ "$bg" = "true" ]; then
  echo "relay-guard: a backgrounded Bash command appears to write into a relay root; the hook cannot lint a target that may not be complete — lint manually before handoff" >&2
  exit 2
fi
if [ -z "$target" ] || [ ! -f "$target" ]; then
  echo "relay-guard: a Bash command appears to have written into a relay root and could not be linted; lint manually before handoff" >&2
  exit 2
fi

case "$(basename "$target")" in
  INDEX.md) mode="--index" ;;
  *) mode="file" ;;
esac
skills_root="${RELAY_LINT_SKILLS_ROOT:-$HOME/.claude/skills}"
rc=0
run_lint() {
  if [ "$mode" = "--index" ]; then
    out="$("$@" --index "$target" 2>&1)" || rc=$?
  else
    out="$("$@" "$target" 2>&1)" || rc=$?
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
  echo "relay-guard: linter not found at any installed location; relay $target UNLINTED" >&2
  exit 2
fi
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -ne 1 ] || printf '%s\n' "$out" | grep -Eq 'Traceback|SyntaxError|ModuleNotFoundError|ImportError|usage:'; then
    echo "relay-guard: linter execution failed for $target:" >&2
  else
    echo "relay-guard: $target FAILS lint:" >&2
  fi
  echo "$out" >&2
  exit 2
fi
exit 0
