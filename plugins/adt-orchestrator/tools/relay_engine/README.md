# Relay engine

Status: the relay engine is adopted and in production use since kit 2.9.1. A
relay root operates in one of two modes, chosen by the operator at run init:
engine-managed (a daemon has cut over and serves it; seats file through
`.engine/drafts/` + `relay submit` and only the daemon writes `INDEX.md`) or
hand-authored (pre-cutover, or rolled back; the D7 ceremony floor files relays
and maintains `INDEX.md`). A root never changes mode implicitly — `relay
migrate` is the only path between the modes. This document describes a relay
root operating under a daemon.

`relay` creates, checks, and records governed relays beneath one canonical
root. Start one daemon for the root before registering seats or submitting
work. The daemon is the designated writer of the run record and of the
rendered `INDEX.md`, relay files, and `SEATS.md` views.

Admission is the only supported path for new work. The importer exists for
legacy migration and bypass detection: imported relays are marked so operators
can distinguish them from daemon-admitted work.

Direct edits to rendered files are detected; they are not blocked. The engine
does not claim exclusive access to the root. If the daemon is unavailable, the
root is inoperable until an eligible starter restores it, except for the single
out-of-band escalation instruction emitted by the command.

## Distribution, activation, and version recovery

Every generated plugin bundle distributes the same engine surface: `tools/relay`, `tools/relay_engine/*.py`, and `tools/relay_engine/README.md`.
Distribution places those bytes in a bundle, but distribution alone activates no consumer.

Claude Code is the only shipped relay-write hook consumer.
Its primary enforcing route is the shared root, normally `$HOME/.claude/skills`, where the configured hooks resolve `tools/`.
Activate or refresh that route with this staged replacement, setting `candidate_tools` to an absolute path to the candidate tree:

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
A marketplace update does not touch the shared-root `tools/` copy, so re-run the shared-root refresh after updating a marketplace plugin.

An advanced Claude Code route may set `RELAY_LINT_SKILLS_ROOT` to a version-qualified plugin root such as `<plugin-root>/adt-master/2.9.0`.
Plugin caches have no stable current-version pointer, so updating a plugin creates a new version directory while an existing configured root continues to name the old directory.
Treat update-plus-repoint as one activation unit: update the plugin and repoint `RELAY_LINT_SKILLS_ROOT` to that new version directory before relying on the new bytes.
The plugin update alone changes nothing enforcing because the configured root still names the old directory.
The repoint alone changes nothing enforcing because it does not install the new bundle.

Codex has no shipped relay-write hook in this kit, so there is no Codex hook consumer to activate.
Codex users and agents run the bundled engine manually as `<plugin-root>/tools/relay`.
Any Codex hook is separate, orchestrator-authorized work and is outside this distribution.

After refreshing any route, verify the actual installed copy before starting or contacting a daemon:

```bash
<plugin-root>/tools/relay version
# or, for the active shared-root route:
$HOME/.claude/skills/tools/relay version
```

The JSON result names the installed `kit`, fingerprint, and resolved `install` path.
The client and daemon may be installed at different paths, but record operations require matching valid kit and fingerprint identities.

If an old client contacts a new daemon, every request refuses with the frozen-client-compatible `E-WIRE-VERSION` code and instructs the operator to update the client install.
Leave the new daemon running, refresh the client route from the same released engine surface, run `relay version` on that client copy, and retry with the refreshed client.

If a new client contacts an old daemon, record operations refuse with `E-VERSION-MISMATCH` and instruct the operator to update the daemon install.
For this legacy direction only, `status` and `daemon stop` retain bounded v1 administrative compatibility so the operator can identify and stop the old daemon.
Use that compatibility only to stop the old daemon, refresh the daemon route from the released engine surface, run `relay version` at both the client and daemon installations, start the refreshed daemon, and retry the record operation.
Do not use the administrative compatibility as a record-operation downgrade or as evidence that the mixed generation is safe.

## Typical flow

1. Run `relay daemon start --root <root> --run-id <run> --seat <top-seat>`.
2. Register the working seats with `relay seat register` and retain each issued
   registration tag file beneath the root.
3. Submit a draft with `relay submit <draft> --key <tag-file>`. Author the draft
   beneath the root (conventionally `.engine/drafts/<seat>/`); the `<draft>`
   argument resolves relative to the root, not the shell working directory, and
   an absolute path must lie inside the root.
   The `--key` argument resolves the same way: root-relative, with an absolute path required to lie inside the root.
   A missing draft or key refuses with a typed `E-PATH-ESCAPE` not-found cause before any admission work.
   To file another relay under an already-used dispatch id, pass `--admits-against <root-relative path of the relay currently holding that id>`.
4. Inspect the derived views with `relay status`, `relay show`, and
   `relay verify`.
5. Stop the designated writer with `relay daemon stop --root <root>`.

Use `relay lint --relay-root <root>` to check an engine-rendered relay tree. It
considers only paths matching the engine's rendered filename grammar and is not
a substitute for the standalone `relay-lint` when checking hand-authored trees.
Migration commands inventory legacy material before any explicit cutover, and
reconcile marks bypassed material without presenting it as a supported creation
path.
