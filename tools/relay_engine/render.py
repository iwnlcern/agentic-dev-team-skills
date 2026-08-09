"""Durable rendering for immutable relays and replaceable projections."""

from dataclasses import dataclass
import hashlib
import os
import re
import stat

from relay_engine.paths import Root, TempWrite


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_PROJECTIONS = {"INDEX.md": "index", "SEATS.md": "seats"}
_INDEX_HEADERS = {
    8: ("time", "phase", "role", "dispatch", "to", "owner", "status",
        "file"),
    10: ("time", "phase", "role", "dispatch", "parent", "from", "to",
         "cc", "status", "file"),
}
_SEATS_HEADERS = ("seat", "role skill", "status", "session", "model/lane")


@dataclass(frozen=True)
class RenderResult:
    event: str
    digest: str
    archive_path: str | None = None
    divergence_digest: str | None = None


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_create(root, rel, data):
    """Publish bytes without replacing any existing directory entry."""
    if not isinstance(root, Root) or root.dirfd is None:
        raise TypeError("open Root required")
    if not isinstance(data, bytes):
        raise TypeError("bytes required")
    with TempWrite(root, rel) as pending:
        pending.write(data)
        pending.rename_noclobber()
    return _digest(data)


def _ensure_archive(root):
    engine_fd = os.open(".engine", _DIRECTORY_FLAGS, dir_fd=root.dirfd)
    try:
        try:
            os.mkdir("archive", 0o700, dir_fd=engine_fd)
            os.fsync(engine_fd)
        except FileExistsError:
            pass
        archive_fd = os.open("archive", _DIRECTORY_FLAGS,
                             dir_fd=engine_fd)
        os.close(archive_fd)
    finally:
        os.close(engine_fd)


def _archive(root, rel, data):
    _ensure_archive(root)
    basename = re.sub(r"[^A-Za-z0-9._-]+", "-", rel.rsplit("/", 1)[-1])
    path_key = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
    content_key = _digest(data)
    archive_path = ".engine/archive/%s-%s-%s.archive" % (
        path_key, basename, content_key)
    try:
        atomic_create(root, archive_path, data)
    except FileExistsError:
        if root.open_read(archive_path) != data:
            raise
    return archive_path


def atomic_replace(root, rel, data, last_digest):
    """Replace one daemon-owned whole-file projection."""
    if rel not in _PROJECTIONS:
        raise ValueError("only INDEX.md and SEATS.md are replaceable")
    if not isinstance(data, bytes):
        raise TypeError("bytes required")
    try:
        current = root.open_read(rel)
    except FileNotFoundError:
        current = None
    divergence_digest = None
    archive_path = None
    if current is not None:
        current_digest = _digest(current)
        if current_digest != last_digest:
            divergence_digest = current_digest
            archive_path = _archive(root, rel, current)
    with TempWrite(root, rel) as pending:
        pending.write(data)
        pending.rename_replace()
    return RenderResult("rendered", _digest(data), archive_path,
                        divergence_digest)


def _event_seq(ledger):
    return ledger.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM projection_events"
    ).fetchone()[0]


def _record_events(ledger, entries):
    own_transaction = not ledger.in_transaction
    if own_transaction:
        ledger.execute("BEGIN IMMEDIATE")
    try:
        for relay_seq, target, event, digest, detail in entries:
            ledger.execute(
                "INSERT INTO projection_events"
                "(seq,relay_seq,target,event,digest,detail) "
                "VALUES(?,?,?,?,?,?)",
                (_event_seq(ledger), relay_seq, target, event, digest,
                 detail))
    except BaseException:
        if own_transaction:
            ledger.execute("ROLLBACK")
        raise
    else:
        if own_transaction:
            ledger.execute("COMMIT")


def render_relay(ledger, root, seq):
    """Render one immutable relay, classifying an occupied final path."""
    row = ledger.execute(
        "SELECT rendered_path,body,body_sha256 FROM relays WHERE seq=?",
        (seq,)).fetchone()
    if row is None:
        raise KeyError(seq)
    rel, body, expected_digest = row
    if not isinstance(body, bytes):
        body = bytes(body)
    if _digest(body) != expected_digest:
        raise ValueError("stored relay body digest mismatch")
    try:
        digest = atomic_create(root, rel, body)
    except FileExistsError:
        occupant = root.open_read(rel)
        digest = _digest(occupant)
        if digest == expected_digest:
            event = "rendered"
            archive_path = None
        else:
            prior = ledger.execute(
                "SELECT 1 FROM projection_events WHERE relay_seq=? "
                "AND event IN ('rendered','repaired') LIMIT 1",
                (seq,)).fetchone()
            if prior is None:
                event = "conflict"
                archive_path = None
            else:
                event = "modified"
                archive_path = _archive(root, rel, occupant)
        _record_events(ledger, [(seq, "relay", event, digest, rel)])
        return RenderResult(event, digest, archive_path)
    _record_events(ledger, [(seq, "relay", "rendered", digest, rel)])
    return RenderResult("rendered", digest)


def _last_projection_digest(ledger, target):
    if ledger is None:
        return None
    row = ledger.execute(
        "SELECT digest FROM projection_events WHERE target=? "
        "AND relay_seq IS NULL AND event IN ('rendered','repaired') "
        "ORDER BY seq DESC LIMIT 1", (target,)).fetchone()
    return None if row is None else row[0]


def _cell(value):
    if value is None:
        return "—"
    return str(value).replace("|", r"\|").replace("\n", " ")


def _table(headers, rows):
    rows = list(rows)
    for row in rows:
        if len(row) != len(headers):
            raise ValueError("projection row arity mismatch")
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |"
                 for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_projection(root, rel, data, epoch, ledger):
    if epoch != "active":
        return None
    target = _PROJECTIONS[rel]
    result = atomic_replace(
        root, rel, data, _last_projection_digest(ledger, target))
    if ledger is not None:
        entries = []
        if result.divergence_digest is not None:
            entries.append((None, target, "divergence",
                            result.divergence_digest, rel))
        entries.append((None, target, "rendered", result.digest, rel))
        _record_events(ledger, entries)
    return result


def render_index(root, rows, arity, epoch, ledger=None):
    if arity not in _INDEX_HEADERS:
        raise ValueError("INDEX arity must be eight or ten")
    data = _table(_INDEX_HEADERS[arity], rows)
    return _render_projection(root, "INDEX.md", data, epoch, ledger)


def render_seats(root, rows, epoch, ledger=None):
    data = _table(_SEATS_HEADERS, rows)
    return _render_projection(root, "SEATS.md", data, epoch, ledger)


def _sweep_dir(fd):
    removed = 0
    changed = False
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            try:
                removed += _sweep_dir(child)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and name.startswith(".relay-tmp-"):
            os.unlink(name, dir_fd=fd)
            removed += 1
            changed = True
    if changed:
        os.fsync(fd)
    return removed


def sweep_tmp(root):
    """Remove abandoned TempWrite regular files without following links."""
    if not isinstance(root, Root) or root.dirfd is None:
        raise TypeError("open Root required")
    fd = os.dup(root.dirfd)
    try:
        return _sweep_dir(fd)
    finally:
        os.close(fd)
