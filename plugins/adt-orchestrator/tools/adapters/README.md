# Agentic Dev Team Adapters

Adapters are optional host hardening. They do not replace the protocol; they make the protocol's mechanical checks run at the right host event.

## Install or refresh the shared root first

Use this staged replacement for an initial install or every refresh of the shared-root `tools/` tree.
Set `candidate_tools` to an absolute path to the candidate tree.
For a Codex-host standalone install, set `skills_root` to `$HOME/.agents/skills`; `~/.codex/skills` still resolves for Codex skill discovery but is deprecated.
The hooks find the linter only through `RELAY_LINT_SKILLS_ROOT`, as described below.

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
Both hooks lint with the engine CLI, `python3 "$RELAY_LINT_SKILLS_ROOT/tools/relay" lint <file>`, when `$RELAY_LINT_SKILLS_ROOT/tools/relay` and `$RELAY_LINT_SKILLS_ROOT/tools/relay_engine/` are present.
When `RELAY_LINT_SKILLS_ROOT` is unset, that root defaults to `$HOME/.claude/skills`.
There is no other location: when the engine CLI is absent, the hooks report the relay `UNLINTED` and exit 2, and a `relay-lint.py` in any older location is never run.
A manual install whose shared root is not `$HOME/.claude/skills`, such as a Codex standalone install under `$HOME/.agents/skills`, sets `RELAY_LINT_SKILLS_ROOT` in each hook command.
They do not probe `relay-lint` on `PATH`.
The write-time hook honors `RELAY_LINT_MAX_DRIFT_MINUTES` (widened authoring-drift tolerance) and `RELAY_LINT_NO_FRESHNESS=1` (skip the freshness check when editing an existing older relay); both default to the strict posture.

## Distribution and activation

Generated plugin bundles ship the relay-write hooks declaration alongside the relay engine and adapter scripts.
The `claude-code/` directory name records the scripts' origin host and payload dialect; these scripts form one dual-host adapter set for Claude Code and Codex.
Plugin-route installs discover `hooks/hooks.json` from the plugin root.
Manual Claude Code installs instead merge `claude-code/settings-snippet.json` after staging `tools/` into the shared skills root.

### Plugin-route pinning and manual-install resolution

Follow `tools/adapters/plugin-hooks.json` for plugin-route pinning: its linter and relay-guard commands assign `RELAY_LINT_SKILLS_ROOT`, while monitor commands pass `--plugin-root`.
The hook therefore uses the byte-matched linter and relay engine shipped in the same plugin version instead of wandering to a stale shared root.
The command-scoped assignment does not modify the user's environment.

Manual installs resolve the linter as documented above, from the configured shared root only.
A marketplace update does not refresh a separately installed shared-root `tools/` copy, so manual users must repeat the staged replacement after updating.

### Consent and migration

Codex presents a content-hash trust prompt when a plugin hook or its script content is first encountered or changes, and a kit update changes it every time.
Codex re-hashes each plugin bundle when a hook runs, so after every kit update each kit hook fails with "plugin contents changed; hook approval required" until it is approved again with `/hooks` (measured at the 2.9.7 install).
Declining keeps the hook disabled; lint every relay manually before handoff as documented in `harness-codex.md`.
Claude Code applies its own plugin-install consent flow.

After switching to the plugin route, remove the legacy entries copied from `settings-snippet.json` from the host settings.
Leaving both routes enabled can produce duplicate lint attempts or advisories.

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

## Shipped adapter: dual-host relay-write hooks

Files:

```text
tools/adapters/claude-code/relay-lint-posttooluse.sh
tools/adapters/claude-code/bash-relay-guard.sh
tools/adapters/claude-code/settings-snippet.json
tools/adapters/claude-code/test-adapter.sh
tools/adapters/plugin-hooks.json
```

Merge the JSON from `settings-snippet.json` into `~/.claude/settings.json` only for a manual Claude Code install after installing `tools/` to the shared skills root.
The snippet invokes each hook as `bash <script>`, so it does not depend on a script's execute bit surviving the copy or extraction.

`route_path` in `tools/adapters/claude-code/relay-lint-posttooluse.sh` decides which Write/Edit/MultiEdit paths are linted, including the `.engine/` draft exemption.
It also extracts relay targets declared by apply-patch envelopes delivered through the Bash or native patch payload forms.
A file whose exact basename is `INDEX.md` is not a relay: the engine renders it, so the hook routes it out before any lint and exits 0.
Other relay Markdown files use explicit-file lint.

The Bash event runs the normalizer and guard additively, without suppression or duplicate elimination.
The guard recognizes literal relay-root writes using redirection, `tee`, `cp`, or `mv`.
When exactly one existing relay Markdown target can be resolved, the guard lints it with the same linter resolution as the normalizer; an `INDEX.md` target exits 0 unlinted.
Multiple, unresolved, or backgrounded targets receive a generic manual-lint advisory without a filename lint claim.

Coverage is observable when the linter is invoked on the exact relay target or when an explicit noisy `UNLINTED` advisory directs manual lint.
A clean target correctly produces silent exit 0 after linting.
Overlapping normalizer and guard coverage can produce duplicate attempts or advisories, which are accepted to avoid silent ownership loss.
Both hooks return exit 2 feedback when advisory attention is needed, distinguish relay-lint failures from linter execution failures, and leave the protocol gates authoritative.

### Host event asymmetry

Claude Code runs the additive Bash handlers for both `PostToolUse` and `PostToolUseFailure`.
Codex recognizes `PostToolUse` and silently ignores the unknown `PostToolUseFailure` key because its hook-event field set is closed without unknown-field denial.
Consequently, a failed Codex Bash command receives no failure-path hook advisory, while successful native patch and Bash events retain their write-time coverage.

Run the hermetic test before enabling:

```bash
bash tools/adapters/claude-code/test-adapter.sh
```

The test builds a temporary skills root from this candidate's `tools/`.
It preserves coverage for clean relay, dirty relay, dirty tripwire relay, non-relay path, non-md relay-root file, `INDEX.md` targets left unlinted, missing-linter degradation (a legacy `relay-lint.py` is never used), and broken-linter attribution.
It also covers literal Bash relay writes, patch-envelope extraction, non-relay writes, heredoc INDEX routing, unresolved move destinations, interpreter residuals, backgrounded writes, write-then-fail delivery, multiple relay targets, exact-basename file routing, additive handler behavior, and the shipped Bash registration under both host events.

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

## Shipped adapter: relay auto-delivery (`relay-monitor.py`)

Files: `tools/adapters/relay-monitor.py`, `tools/adapters/window.py`, `tools/adapters/hostlogs.py`, `tools/adapters/shell_lex.py`, `tools/adapters/plugin-monitors.json` (shipped as `monitors/monitors.json`), the three hooks in `plugin-hooks.json`, `tools/adapters/codex/adt-codex`, `tools/adapters/claude-code/relay-guard-destinations.py`, tests and the scope oracle in `tools/adapters/tests/`.

Modes: `follow` (the watcher; plugin monitor on Claude Code, fork `monitor start` on Codex), `bind` (PostToolUse hook and `--manual` rebind), `drain` (Codex Stop hook), `session-start` (delivery state into context; fork arming), `status`, `operator`, `replay`.
State: one note per session under `${TMPDIR:-/tmp}/adt-relay-monitor/<session>.json` with a short state lock, and a lifetime window lock `window-<key>.lock` in the same directory (one doorbell per window, below); progress is a cursor on the index's `file` cell.
The progress rule in `bind_from_receipt` in `tools/adapters/relay-monitor.py` applies only to filings it binds from.
For those filings, one as the bound seat in the bound root leaves progress where it is; any other moves that root's progress to the filing itself, skipping earlier undelivered relays even after a return filing.
For filings it does not bind from, binding and progress stay where they were, and the hook prints a rebind line for only some of them; `relay-monitor.py status` shows where the watcher is bound, and the absence of a rebind line does not.
Check `relay-monitor.py status` for the intended root before trusting `relay-monitor.py replay`'s answer: replay re-prints skipped relays only for the root the watcher is bound to, together with ones already delivered.
Check each against work already handled.
Override: `ADT_SEAT` and `ADT_RELAY_ROOT` (both required; `ADT_RELAY_ANCHOR` optional) bind without a hook, for hand-authored roots; `ADT_HOST` names the host explicitly.

The status grammar separates follower readiness from binding observations and install identity:

```text
relay-monitor: <word>[: <reason>] seat=<s> root=<r> cursor=<c> binding=<hook|manual|env|none> window=<key> doorbell=<kit>@<sha> client=<id> daemon=<id>[ engine=wedged] updated=<stamp>[ miss=submit-near-miss][ last-bind=<reason>][ identity=mismatch]
[remedy: <one line>]
[rebind: <one line>]
```

The five readiness words are `not-started` (no doorbell holds the window lock and no follower exit is recorded), `starting` (the lock holder and leader record do not yet agree), `waiting-for-binding` (a live leader without initialized binding progress), `armed` (a live leader following initialized progress), and `unavailable` (a recorded delivery fault or follower exit, with exit reason `transport-failed`, `follower-error`, `window-gone` or `terminated`).
A binding observation changes neither readiness nor delivery eligibility.
Delivery requires a binding, a usable index and cursor or fallback anchor, an addressed row beyond that position, and an available output path; the Codex drain can deliver without a follower.
A missing cursor and fallback anchor produce `unavailable: anchor-not-found`; index faults report `index-missing`, `index-malformed`, `index-unreadable` or `root-stalled`.
A healthy scan clears an index fault, while only a new follower reaching following clears a recorded exit, so a successful drain does not clear that exit.
A follower bound by environment with no anchor, starting on a root it has no progress for while that root's index exists but is unreadable, stalled or malformed, begins at the first readable moment, so rows filed during that outage are not delivered; a missing index, and an anchored start, keep their base delivery.
The `binding=` cell reports only the binding source.
A new near-miss observation supplies `miss=submit-near-miss`; another new reason supplies `last-bind=<reason>`.
Legacy miss and bind-failure keys are displayed only, with the new record taking precedence in its cell; a legacy value in the other cell can coexist.
An identity mismatch adds `identity=mismatch` and a remedy on every reading, preserving the readiness word and delivery; the seat must apply the remedy before submitting.

A command that parses as a submit or contains adjacent unquoted relay and submit words outside a heredoc body, with a receipt in its response, records an observation when it cannot bind.
No receipt means no observation or binding change, including help, refused submits and redirected output.
A displayed receipt or an unexecuted branch can therefore produce a suspicion even when nothing was filed; this does not bind the seat.
The hook emits a bounded `PostToolUse` additional-context hint with exit 0 when it records a new observation.
The three hint forms are:

```text
rebind, may need operator approval: python3 <adapter-path> bind --manual --session <session> --root <root> --anchor <receipt-path>
rebind (root inferred, check it, may need operator approval): python3 <adapter-path> bind --manual --session <session> --root <root> --anchor <receipt-path>
# rebind: incomplete, supply --root and --anchor seen=...
```

Both runnable hints note that the command may need operator approval; the text after the first `: ` is the copyable command.
The complete form uses the root parsed from the command and shell-quotes every argument as needed.
The inferred form requires exactly one candidate root with the receipt's index row and asks the operator to check it.
Both runnable forms recheck the stored root's index on every rendering; a missing row or unreadable index gives the incomplete form until the row can be read.
The index read is a bound-root call within its operation’s 1 s budget and reports `root-stalled` if it does not finish within that budget.
The incomplete form is not runnable and never substitutes the existing binding anchor for the observed receipt.
When lexical root indications contradict inference, the incomplete hint shows their bounded, shell-quoted operand spellings as command text; this optional stored text never drives binding or delivery.
Hints are at most 1,024 characters; an oversized or multiline command uses the incomplete form.
An oversized incomplete hint drops whole quoted values from the end and appends `dropped=N`, the number removed, so truncation never leaves an open quote.
A manual bind clears observations even at the current anchor, without advancing the generation there; it preserves existing progress for the same seat and root.
A hook reporting the current seat, root and anchor with progress already present is a no-op and clears nothing.
A changed binding clears earlier observations inside its transaction; a later observation survives.

For hosts supplying `tool_use_id`, the last eight session-and-call identities suppress duplicate tier-hook reports inside one note transaction.
A different call with the same content is a different invocation; without the identifier only consecutive duplicate binds or observations collapse.
The guarantee applies only when all hooks use this version: the mixed sequence new hook, 2.9.6 hook, 2.9.6 hook advances the generation by three.
Numeric fd duplication and close, including `2>&1`, `>&2` and `2>&-`, are accepted while a background ampersand remains unsupported.
The relay guard retains its existing limitation for a file operand of `>&`, such as `>& relay.md`; this change does not add destination detection for that form.

Inbound delivery reads the run's rendered `INDEX.md` without a daemon socket call.
For the follower's local engine identity invocation, see `leader_record` in `tools/adapters/relay-monitor.py`.
A seat that also files its own relays needs a sandbox profile that permits the relay daemon's socket.
A seat under a profile that refuses the socket binds its watcher once at setup with `python3 <plugin-root>/tools/adapters/relay-monitor.py bind --manual --session <session-id> --root <run-root> --anchor <relay-file>`, and every later delivery needs no action.
The anchor is the root-relative `file` cell of an `INDEX.md` row this seat authored, because manual binding takes the seat from that row's `FROM` cell.
A successful manual bind exits 0 with empty stdout and stderr; a refusal exits 1 with one reason line on stderr and leaves the note unchanged.
Missing root or anchor arguments, or an invalid session, exit 2 with a diagnostic; unexpected errors are not swallowed.
Confirm the binding with `python3 <plugin-root>/tools/adapters/relay-monitor.py status --session <session-id>`.
Without `--session`, the command uses `CLAUDE_CODE_SESSION_ID` when set, otherwise `CODEX_THREAD_ID`, and reports an invalid session without writing when the resolved value is missing or invalid.
A seat under such a profile that sees `E-DAEMON-DOWN` from its own `tools/relay` call is most likely hitting that socket refusal rather than a stopped daemon; confirm the daemon from a seat that permits the socket before anyone restarts it.

Test instrumentation (read once at import, default off, never set in production): `ADT_TEST_CRASH_BEFORE_OUTPUT`, `ADT_TEST_CRASH_AFTER_FLUSH`, `ADT_TEST_CRASH_AFTER_OUTPUT` exit the process with status 9 at the named point; `ADT_TEST_SINK_ACCEPT` caps, process-wide, the total number of bytes the delivery sink accepts (a write is truncated to the remaining room and reports zero progress once the cap is spent), which the tests use to force an output stall; `ADT_TEST_PAUSE_BEFORE_CLEANUP` names a file that a Stop drain aborted by an output stall waits for, at most ten seconds, before its stall cleanup, which the tests use to hold the drain at that point; `ADT_PACE_LINES` and `ADT_PACE_WINDOW` override the pacing budget.

Operator notifications on macOS: run `python3 <plugin-root>/tools/adapters/relay-monitor.py operator --root <run-root>` in a terminal, or install this launch agent as `~/Library/LaunchAgents/com.adt.relay-monitor.operator.plist` (validate with `plutil -lint`, load with `launchctl load`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.adt.relay-monitor.operator</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/USER/.claude/plugins/cache/agentic-dev-team-skills/adt-orchestrator/VERSION/tools/adapters/relay-monitor.py</string>
    <string>operator</string><string>--root</string><string>/absolute/path/to/docs/sprints/active/RUN/.relays/RUN_ID</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USER/Library/Logs/adt-relay-monitor/operator.log</string>
  <key>StandardErrorPath</key><string>/Users/USER/Library/Logs/adt-relay-monitor/operator.err</string>
</dict></plist>
```

The stdout log is the durable record of notified rows; the notification itself is best-effort.

`rebind_line` in `tools/adapters/relay-monitor.py` formats incomplete hints as quoted, escaped diagnostic text.
With the tested Bash 3.2.57 defaults, the leading `#` makes the pasted line a comment.
Interactive zsh 5.9 without `INTERACTIVE_COMMENTS` instead attempts to run `#` as a command and reports command-not-found; the quoted receipt and operand values remain inert.
Do not treat an incomplete hint as a runnable rebind command.
Complete and inferred commands are emitted only for printable ASCII arguments; other text falls back to an escaped incomplete hint.
Status fields also escape terminal controls and non-ASCII characters.

### One doorbell per window

A window is the agent process the seat runs in.
On Claude Code it is the nearest ancestor process with a host record `~/.claude/sessions/<pid>.json` whose `pid` and `procStart` match that process; the record is an undocumented Claude Code internal (measured on 2.1.318), so when no record matches, the window is the first non-shell ancestor above the nearest run of shells.
On Codex the window is `CODEX_THREAD_ID`.
`ADT_HOST` names the host; otherwise `CODEX_THREAD_ID` selects Codex, because a Codex environment can inherit `CLAUDE_CODE_SESSION_ID`.
A doorbell that cannot determine its window keys on its session, as before.
Each doorbell takes the window's lock without waiting; a second doorbell in the same window, such as another tier's plugin monitor, exits 0 with no output and leaves no record, so there are no standbys.
`window=` in the status line names the window key, and `doorbell=` names the running doorbell's own kit version and the first eight hex digits of its `relay-monitor.py` sha256, taken from the doorbell's record rather than from the process running `status`.

### Doorbell exits

A doorbell exits when its Claude Code window is gone (the window's process has exited, or its pid now names a process with another start time) or when it receives SIGTERM, SIGHUP or SIGINT, including during its own startup.
Each exit prints exactly one line on stdout, `relay-monitor: stopped: window gone` or `relay-monitor: stopped: terminated`, records the exit in the note, frees the window lock and exits 0.
A stopping doorbell writes no further arrival, and an arrival it wrote after the stop request is offered again by the next doorbell, so a stop loses no arrival and can repeat one.
The window's exit is reported by the kernel (kqueue on macOS, a pidfd on Linux), so an idle doorbell starts no process to check it; the window's start time is read once, when the doorbell starts.
An engine outage is not an exit: the doorbell keeps polling.

### Bounded status

`status` and SessionStart make every read under one 1.8 s deadline, with the engine's identity checks under a shared 1.5 s part of it, so both return within 2 s on a wedged engine, a hung `ps` or a busy note; a timed-out engine check shows `daemon=-` with `engine=wedged`.
Neither takes the window lock or waits on the note's lock: the note is read whole without it, and a write that cannot get it in time is skipped.
The answer is cached in the note for 60 s per root and plugin root; a wedged answer is never cached.

### Host token logs

`python3 tools/adapters/hostlogs.py read --host <claude-code|codex> [--owners <file>] <path>...` prints one JSON object per turn from host logs: token counts and identifiers only, never message content.
`input` is uncached input on both hosts (Codex `input_tokens` minus `cached_input_tokens`), and `output` includes reasoning on both hosts.
Claude Code rows are one per `requestId`; `--owners` keeps a forked session's copied requests with the session that first owned them.
Within one read, a request copied into several sessions belongs to the session whose own first usage line is earliest, then to the file created first where the filesystem records creation times, then to the smaller session id.
Codex rows are one per `response_id`, keyed by the rollout's own thread (its first `session_meta`); a record naming another thread is a copy of that thread's record and is skipped; `.bak` rollouts are skipped and a rollout without a `token_usage_record` of its own is reported as missing.

### Host facts

These host behaviours were measured on this kit's hosts and govern installs and upgrades.

- Running sessions keep the plugin version they started with: Claude Code keeps the old tree under `.in_use`, so a kit update reaches a seat only when its session restarts.
- `/reload-plugins` reloads hooks and skills but starts no new plugin monitor, so a reloaded session's doorbell stays on the old version.
- Restart a root's relay daemon on the new kit only after that root's sessions restart; a daemon ahead of its sessions refuses their filings with `E-VERSION-MISMATCH`.
- `codex plugin add` replaces the plugin cache, deleting the previous version's tree while running Codex seats still execute from it; back the cache up first and restart or re-arm Codex seats right after.
- Codex re-approves kit hooks after each update (see Consent and migration).
- A watch started with Claude Code's Monitor tool, rather than as a plugin monitor, ends when its timeout expires, at most 30 minutes; re-arm it and read the root's `INDEX.md` for relays filed in the gap.
- One doorbell runs per window, and each doorbell exit prints one `relay-monitor: stopped:` line (above).
