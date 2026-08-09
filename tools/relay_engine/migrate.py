"""Migration epoch primitives shared by startup recovery and maintenance."""

import hashlib
import json

from relay_engine.jcs import jcs_encode
from relay_engine.paths import TempWrite


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def rollback_detail(archive, phase):
    if phase not in {"inert", "restored"}:
        raise ValueError("rollback phase mismatch")
    return jcs_encode({"archive": archive, "phase": phase}).decode("utf-8")


def _parse_detail(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("rollback detail mismatch") from exc
    if (not isinstance(parsed, dict) or set(parsed) != {"archive", "phase"}
            or rollback_detail(parsed["archive"], parsed["phase"]) != value):
        raise ValueError("rollback detail mismatch")
    return parsed


def pending_rollback(ledger):
    """Return the latest incomplete inert rollback, if any."""
    rows = ledger.execute(
        "SELECT seq,digest,detail FROM migration_events "
        "WHERE event='rollback' ORDER BY seq").fetchall()
    latest = None
    restored = []
    for seq, digest, detail in rows:
        parsed = _parse_detail(detail)
        if parsed["phase"] == "inert":
            latest = (seq, digest, parsed["archive"])
        elif parsed["phase"] == "restored":
            restored.append((seq, digest, parsed["archive"]))
    if latest is None:
        return None
    seq, digest, archive = latest
    if any(row_seq > seq and row_digest == digest and row_archive == archive
           for row_seq, row_digest, row_archive in restored):
        return None
    return {"seq": seq, "digest": digest, "archive": archive}


def complete_pending_rollback(ledger, root):
    """Idempotently restore INDEX bytes for an interrupted rollback."""
    pending = pending_rollback(ledger)
    if pending is None:
        return None
    archived = root.open_read(pending["archive"])
    if _digest(archived) != pending["digest"]:
        raise ValueError("rollback archive digest mismatch")
    try:
        live = root.open_read("INDEX.md")
    except FileNotFoundError:
        live = None
    if live is None or _digest(live) != pending["digest"]:
        with TempWrite(root, "INDEX.md") as write:
            write.write(archived)
            write.rename_replace()
    ledger.execute("BEGIN IMMEDIATE")
    try:
        seq = ledger.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM migration_events"
        ).fetchone()[0]
        ledger.execute(
            "INSERT INTO migration_events(seq,event,digest,detail) "
            "VALUES(?,'rollback',?,?)",
            (seq, pending["digest"],
             rollback_detail(pending["archive"], "restored")))
    except BaseException:
        ledger.execute("ROLLBACK")
        raise
    else:
        ledger.execute("COMMIT")
    return pending
