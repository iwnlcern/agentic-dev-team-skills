# Relay engine

Status: the relay engine is not adopted. The shipped skills workflow remains
current practice; this document describes a relay root operating under a daemon.

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
