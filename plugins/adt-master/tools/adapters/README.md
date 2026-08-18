# Agentic Dev Team Adapters

Adapters are optional host hardening. They do not replace the protocol; they make the protocol's mechanical checks run at the right host event.

## Install or refresh the shared root first

Use this staged replacement for an initial install or every refresh of the shared-root `tools/` tree.
Set `candidate_tools` to an absolute path to the candidate tree.
For a Codex-host standalone install, set `skills_root` to `$HOME/.agents/skills`; `~/.codex/skills` remains a working deprecated fallback destination.

```bash
(
set -eu
candidate_tools="/absolute/path/to/candidate/tools"
skills_root="$HOME/.claude/skills"
active_tools="$skills_root/tools"
mkdir -p "$skills_root"
next_tools="$(mktemp -d "$skills_root/.tools.next.XXXXXX")"
backup_tools="$(mktemp -d "$skills_root/.tools.previous.XXXXXX")"
rmdir "$next_tools" "$backup_tools"
cp -R "$candidate_tools" "$next_tools"
if [ -e "$active_tools" ] || [ -L "$active_tools" ]; then
  mv "$active_tools" "$backup_tools"
else
  backup_tools="none"
fi
mv "$next_tools" "$active_tools"
printf 'active=%s\nbackup=%s\n' "$active_tools" "$backup_tools"
)
```

The candidate is copied to a unique sibling before the active name changes, so the replacement cannot nest `tools/tools` or retain files absent from the candidate.
When an active tree exists, the printed `.tools.previous.*` path is the recoverable previous tree; restore it by moving the failed active tree aside and renaming that backup to `tools`.

`<skills-root>` means the skills root the installed skill tree was resolved from — each generated plugin tree ships its own `tools/`.
Both hooks first resolve the engine when `$RELAY_LINT_SKILLS_ROOT/tools/relay` and `$RELAY_LINT_SKILLS_ROOT/tools/relay_engine/` are present.
When `RELAY_LINT_SKILLS_ROOT` is unset, that root defaults to `$HOME/.claude/skills`.
They then resolve `$RELAY_LINT_SKILLS_ROOT/tools/relay-lint.py`, `$HOME/.agents/skills/tools/relay-lint.py`, and `$HOME/.codex/skills/tools/relay-lint.py` (deprecated), in that order.
They do not probe `relay-lint` on `PATH`.
The write-time hook honors `RELAY_LINT_MAX_DRIFT_MINUTES` (widened authoring-drift tolerance) and `RELAY_LINT_NO_FRESHNESS=1` (skip the freshness check when editing an existing older relay); both default to the strict posture.

## Distribution and activation

Generated plugin bundles distribute the relay engine, but installing or updating a bundle does not activate a hook by itself.
The shipped relay-write hooks are Claude Code-only and enforce through the root their settings resolve.
The primary enforcing route is the shared root, so activate it with the staged replacement above and merge the Claude Code settings snippet afterwards.
A marketplace update does not touch the shared-root `tools/` copy, so refresh that copy explicitly after every marketplace update.

For the advanced plugin-root route, plugin caches are version-qualified and have no stable current-version pointer.
Use update-plus-repoint as one operation: update the plugin and set `RELAY_LINT_SKILLS_ROOT` to the new `<plugin-root>/adt-<tier>/<version>` directory.
An update without that repoint leaves the hook enforcing from its old configured root.
A repoint without the update supplies no new engine bytes.

Codex has no shipped relay-write hook in this kit and therefore has no hook activation procedure.
Codex manual and agent consumers invoke the installed bundle directly as `<plugin-root>/tools/relay`.

Verify a refreshed route with its own executable before use:

```bash
<plugin-root>/tools/relay version
# or, for the shared Claude Code route:
$HOME/.claude/skills/tools/relay version
```

The command reports the kit, engine fingerprint, and resolved install path.
If a client and daemon report incompatible kit or fingerprint identities, record operations refuse with `E-VERSION-MISMATCH`.
For an old client and new daemon, refresh the client and retry while leaving the daemon running.
For a new client and old daemon, use only legacy `status` or `daemon stop` compatibility to stop the old daemon, then refresh and restart the daemon before retrying.
The engine README gives the full mixed-generation recovery walk-through.

## Shipped adapter: Claude Code relay-write hooks

Files:

```text
tools/adapters/claude-code/relay-lint-posttooluse.sh
tools/adapters/claude-code/bash-relay-guard.sh
tools/adapters/claude-code/settings-snippet.json
tools/adapters/claude-code/test-adapter.sh
```

Merge the JSON from `settings-snippet.json` into `~/.claude/settings.json` after installing `tools/` to the shared skills root. The snippet invokes each hook as `bash <script>`, so it does not depend on a script's execute bit surviving the copy/extract — a `cp -R` or `unzip` that drops the `+x` mode no longer breaks the hooks.

The Write/Edit hook lints relay `*.md` files under a `.relays/` or visible `relays/` path and skips non-relay and non-md writes. It routes a file whose exact basename is `INDEX.md` through relay-lint's `--index` mode; other relay Markdown files use explicit-file lint.

The Bash guard is registered under both `PostToolUse` and `PostToolUseFailure`. It recognizes literal relay-root writes using redirection, `tee`, `cp`, or `mv`. When exactly one existing relay Markdown target can be resolved, the guard lints it with the same linter-location chain as the Write/Edit hook. Multiple, unresolved, or backgrounded targets receive a generic manual-lint advisory without a filename lint claim. Both hooks return exit 2 feedback when advisory attention is needed, distinguish relay-lint failures from linter execution failures, and never hard-block; protocol gates remain authoritative.

Run the hermetic test before enabling:

```bash
bash tools/adapters/claude-code/test-adapter.sh
```

The test builds a temporary skills root from this candidate's `tools/`. It preserves coverage for clean relay, dirty relay, dirty tripwire relay, non-relay path, non-md relay-root file, explicit INDEX mode, missing-linter degradation, and broken-linter attribution. It also covers literal Bash relay writes, non-relay writes, heredoc INDEX routing, unresolved move destinations, interpreter residuals, backgrounded writes, write-then-fail delivery, multiple relay targets, exact-basename file routing, and the shipped Bash registration under both host events.

## Adapter family design

### Auto-lint at relay-write time — shipped

Evidence: transport-compliant relays with freelanced bookkeeping repeatedly reached review. The linter catches the drift when run; this adapter makes it run when relay files are written.

### Adapter residuals — known limits

The Bash advisory guard recognizes literal paths only; variable/`cd`-relative paths, interpreter one-liners, `sed -i`, `git apply`/`checkout`, symlinked roots, and backgrounded-command final state are not verified; advisory coverage, not containment.

Any `.md` under a directory named `relays/` may draw a non-blocking advisory even when it is not a relay; this is bounded by the hooks' exit-2 feedback posture.

Notebook tooling and other host channels remain outside these hooks. The Write/Edit hook matches `*.md` under `.relays/` or `relays/` by convention, so `.markdown` files are skipped. Protocol gates and explicit relay-lint runs remain authoritative.


### Git-state enforcement against silent merges — designed, deferred

Evidence source: git state when available. Future host adapters can check for a merge-authorization relay with the same `DISPATCH_ID` before allowing a merge/push. The addressed `DISPATCH MERGE` protocol layer ships first; harness enforcement remains defense-in-depth until a post-token failure licenses implementation.

### Actor identity / forged FROM — designed, deferred

Truth-agnostic relay-lint cannot prove which process wrote a relay. Harness identity can use per-session relay-write namespaces and host identity hooks. No observed forged-FROM failure licenses implementation beyond the structural anti-self-authorization already shipped.

### Per-phase tool permissioning — designed, deferred

Hosts can map ROLE+PHASE to permission profiles: read-only for AUDIT/DESIGN/PLAN/PLAN-REVIEW, write only after valid dispatch, fold-write only on the PR branch, and merge under operator control. No new observed failure licenses a specific adapter in this package.

## Host-reality requirements

Adapters must state evidence source per condition. Code/worktree claims use git when available; relay-file existence and lint use filesystem paths because `.relays/` may be globally gitignored. If git is unavailable or sandbox-restricted, use `unavailable — <reason>` rather than fabricating proof.

Observed degraded-host realities: Codex workspace-write can block `.git` writes; `.relays/` can be gitignored; wrapper tooling can mask byte diffs. Evidence-grade comparisons use `/usr/bin/git`, `cmp`, `sha256sum`, or direct filesystem reads, not hooked wrappers.

Deployment drift remains on the watch list. A future adapter hardening turn may add a relay-lint version handshake. It keeps the cheaper rule: install `tools/` under the shared skills root and use `RELAY_LINT_SKILLS_ROOT` for hermetic tests and nonstandard installs.
