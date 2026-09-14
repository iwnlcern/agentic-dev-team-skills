# Codex harness notes

Read this when your harness is OpenAI Codex (CLI, IDE extension, or cloud).
Claude Code is this kit's default harness and has no such file.

## Skill loading

- Codex discovers skills from `.agents/skills` (project) or `~/.agents/skills` (user); `~/.codex/skills` still resolves but is deprecated.
- There is no Skill tool on this host; invoke a skill explicitly with `$<skill-name>` or by reading its `SKILL.md` directly.
- Frontmatter beyond `name` and `description` is inert on this host: `allowed-tools` and skill-local hook metadata are not enforced here, so every gate they would back binds behaviorally instead.

## Subagents and review panels

- Current Codex releases enable subagent workflows by default; the control is `agents.enabled` in `~/.codex/config.toml`, default `true`.
- When delegation is disabled or unavailable on this host, run review panels inline and sequentially, one lens at a time.

## Git and sandbox realities

- A workspace-write Codex sandbox can block `.git` writes; when that happens `git status --short` cannot run and the protocol's `unavailable — <reason>` form applies.
- Sandboxed wrapper tooling on this host can mask byte diffs; prefer `/usr/bin/git`, `cmp`, `sha256sum`, or direct file reads for evidence-grade comparison.
- Detached HEAD in a managed Codex sandbox means branch/push/PR are unavailable; commit the work and report, so the operator can finish through the host's own controls.

## relay-lint

- Plugin-route installs ship the relay-write adapter through `hooks/hooks.json`; Codex presents a one-time content-hash trust prompt when the hook or its script bytes are first encountered or change.
- Manual installs and users who decline that prompt run the linter before every handoff: `python3 <skills-root>/tools/relay-lint.py <relay>`, where `<skills-root>` is the directory the skills were installed into.

## Instructions files

- `AGENTS.md` is this host's default project-instructions file, concatenated root-downward with closer files refining earlier ones; `CLAUDE.md` is not read unless configured via `project_doc_fallback_filenames`.

## Relay auto-delivery

- The supported host for auto-delivery is the kit's Codex fork (`iwnlcern/codex`, DD-v295-b3): the `monitor` tool carries `relay-monitor.py follow` for the seat.
  The kit never probes for the fork: on every Codex seat the SessionStart hook supplies the arming context, and the boot turn arms only if `monitor` is in its advertised tool surface, otherwise reporting `unavailable: monitor-tool` with the pointer fallback.
  Set `ADT_CODEX_FORK=0` in a seat's environment to suppress the arming context on a seat you know is stock.
- Install per the fork's recipe in DD-v295-b3 6.11 (the npm vendor slot); `codex features list` and the recorded sha256 are install evidence for the operator and never gate arming.
- The fork forwards at most 4 KiB of text per record; a row over that size is unsupported for monitor delivery and is recovered through `relay-monitor.py replay`, which reads the index directly.
- Start a seat through the shipped launcher `tools/adapters/codex/adt-codex`, which exports `ADT_HOST=codex` and execs the binary; it is the explicit host marker the hooks read, and it must not be exported globally on a machine that also runs Claude seats.
- The plugin's `hooks/hooks.json` adds three hooks: SessionStart (`session-start`, which prints the delivery state and, on the fork, the arming instruction), PostToolUse `Bash` (`bind`, which records the seat after every `tools/relay submit` with a visible receipt), and Stop (`drain`, which continues the turn with one pending arrival).
- New or changed hooks are skipped until trusted: run `/hooks` once per seat after installing or upgrading and trust the three relay-monitor entries; plugin hooks are not auto-trusted.
- On this host hook processes receive `session_id` on stdin, not in the environment; the arming instruction therefore passes `--session <id>` explicitly.
- The fork forwards the watcher's stderr in a tagged block of the same notification, so `follow` writes nothing to stderr after startup; diagnostics live in the note under `${TMPDIR:-/tmp}/adt-relay-monitor/`.
- Stock Codex is fallback-only: the seat's acknowledgment says `fallback: pointer`, relays reach it by the operator's pointer block, and `codex queue --thread <id> --message <text>` is the documented manual alternative.
- A shell call that fails after a successful `relay submit`, or a submit with redirected stdout, is not bound by hook on any host; rebind with `python3 <plugin-root>/tools/adapters/relay-monitor.py bind --manual --session <id> --root <run-root> --anchor <file-cell>`.
