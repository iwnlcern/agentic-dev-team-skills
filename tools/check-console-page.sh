#!/bin/sh
# Run the independent fixture and browser walks without the engine test suite.
set -eu
fail() { printf '%s\n' "console page walk: $1" >&2; exit 2; }
[ -n "${ADT_PAGE_TOOLCHAIN:-}" ] || fail 'set ADT_PAGE_TOOLCHAIN'
[ -d "$ADT_PAGE_TOOLCHAIN" ] || fail 'toolchain folder missing'
command -v node >/dev/null 2>&1 || fail 'Node 20 or later required'
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' 2>/dev/null || fail 'Node 20 or later required'
node -e 'const fs=require("node:fs"); const p=JSON.parse(fs.readFileSync(process.argv[1],"utf8")); process.exit(p.version === "1.62.1" ? 0 : 1)' "$ADT_PAGE_TOOLCHAIN/node_modules/playwright/package.json" 2>/dev/null || fail 'Playwright 1.62.1 required'
PLAYWRIGHT_BROWSERS_PATH="$ADT_PAGE_TOOLCHAIN/browsers"
export PLAYWRIGHT_BROWSERS_PATH
node -e '
const fs=require("node:fs"), path=require("node:path");
const p=process.env.PLAYWRIGHT_BROWSERS_PATH;
const entries=fs.readdirSync(p,{withFileTypes:true});
const shell=entries.filter(x=>/^chromium_headless_shell-/.test(x.name));
const ffmpeg=entries.filter(x=>/^ffmpeg-/.test(x.name));
const allowed=x=>x.isDirectory() && (/^chromium_headless_shell-/.test(x.name) || /^ffmpeg-/.test(x.name) || x.name === ".links");
process.exit(shell.length === 1 && ffmpeg.length <= 1 && entries.every(allowed) ? 0 : 1);
' 2>/dev/null || fail 'unexpected or missing browser folder'
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
FIXTURE="$ROOT/tools/relay_engine/tests/console_page_fixture.py"
WALK="$ROOT/tools/relay_engine/tests/console_page_walk"
[ "$#" -gt 0 ] || set -- smoke
for scenario do
    [ "$scenario" = smoke ] || fail "unknown scenario: $scenario"
done
TMP=$(mktemp -d)
fixture_pid=
cleanup() {
    if [ -n "$fixture_pid" ]; then
        kill "$fixture_pid" 2>/dev/null || :
        wait "$fixture_pid" 2>/dev/null || :
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
PYTHONDONTWRITEBYTECODE=1 python3 "$FIXTURE" --origin-file "$TMP/origin" --page-dir "$WALK/smoke_page" > "$TMP/fixture.log" 2>&1 &
fixture_pid=$!
i=0
while [ ! -f "$TMP/origin" ]; do
    if ! kill -0 "$fixture_pid" 2>/dev/null; then
        cat "$TMP/fixture.log" >&2
        exit 1
    fi
    i=$((i + 1))
    [ "$i" -lt 100 ] || { cat "$TMP/fixture.log" >&2; exit 1; }
    sleep 0.05
done
status=0
node "$WALK/walk.mjs" "$TMP/origin" "$@" > "$TMP/walk.log" 2>&1 || status=$?
cat "$TMP/walk.log"
# node:test may print its footer after the walk's line; make the receipt last.
sed -n '/^console page walk: [0-9][0-9]* passed, [0-9][0-9]* failed$/p' "$TMP/walk.log"
exit "$status"
