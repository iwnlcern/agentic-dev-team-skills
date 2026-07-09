# Agentic Dev Team Adapters

Adapters are optional host hardening. They do not replace the protocol; they make the protocol's mechanical checks run at the right host event.

## Install first

Copy the entire `tools/` directory to the shared skills root before enabling adapters:

```bash
cp -R tools "$HOME/.claude/skills/tools"
# or for Codex-host installs:
cp -R tools "$HOME/.codex/skills/tools"
```

`<skills-root>` means the directory containing the four role skill folders, not an individual role directory. The Claude Code hook resolves relay-lint in this order: `$RELAY_LINT_SKILLS_ROOT/tools/relay-lint.py`, `$HOME/.codex/skills/tools/relay-lint.py`, then `relay-lint` on `PATH`.

## Shipped adapter: Claude Code relay-lint PostToolUse hook

Files:

```text
tools/adapters/claude-code/relay-lint-posttooluse.sh
tools/adapters/claude-code/settings-snippet.json
tools/adapters/claude-code/test-adapter.sh
```

Merge the JSON from `settings-snippet.json` into `~/.claude/settings.json` after installing `tools/` to the shared skills root. The snippet invokes the hook as `bash <script>`, so it does not depend on the script's execute bit surviving the copy/extract — a `cp -R` or `unzip` that drops the `+x` mode no longer breaks the hook. The hook watches Write/Edit outputs. It lints relay `*.md` files under a `.relays/` path, skips non-relay and non-md writes, and explicitly skips `.relays/**/INDEX.md` because INDEX.md is bookkeeping rather than a single relay. It returns exit 2 feedback when lint fails or the linter is unavailable. It distinguishes relay-lint failures from linter execution failures such as a broken Python tool or traceback. It does not hard-block; protocol gates remain authoritative.

Run the hermetic test before enabling:

```bash
bash tools/adapters/claude-code/test-adapter.sh
```

The test builds a temporary skills root from this candidate's `tools/`, covers clean relay, dirty relay, dirty tripwire relay, non-relay path, non-md relay-root file, INDEX.md skip, missing-linter degradation, and broken-linter attribution.

## Adapter family design

### Auto-lint at relay-write time — shipped

Evidence: transport-compliant relays with freelanced bookkeeping repeatedly reached review. The linter catches the drift when run; this adapter makes it run when relay files are written.

### Adapter residuals — known limits

The Claude Code PostToolUse hook watches Write/Edit file outputs only. Relay files written through Bash/heredoc redirection, notebook tooling, rename-into-place, or another host channel may bypass the hook; protocol gates and explicit relay-lint runs remain authoritative. The hook also matches `*.md` under `.relays/` by convention; `.markdown` files under `.relays/` are skipped. `.relays/**/INDEX.md` is skipped because it fails single-relay lint by design and is only run bookkeeping.


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
