"""Noncommitting import of 2.9.7 history (Store amendment 1 § 4.1–4.3)."""

from dataclasses import dataclass
import hashlib
import re
import sqlite3

from relay_engine import header, jcs, submit


@dataclass(frozen=True)
class ImportedRelay:
    created_at: int
    kind: str
    verdict: str | None
    subject: str
    sender: str
    sender_role: str | None
    team_instance_id: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    parent_id: str | None
    parent_spelling: str | None
    body: bytes
    header_bytes: int
    header_sha256: str


def import_relay_step(conn, project_id: str, relay: ImportedRelay, *, marked_at_ms: int) -> str:
    """Write one imported relay inside the caller's transaction, without admission."""
    if conn in submit._READ_GUARDS:
        raise sqlite3.DatabaseError("import is not allowed during a read guard")
    if conn in submit._HOOK_FRAMES:
        raise sqlite3.DatabaseError("import is not allowed during a hook")
    if conn in submit._CHECK_FRAMES:
        raise sqlite3.DatabaseError("import is not allowed during a sender check")
    if not conn.in_transaction:
        raise ValueError("import needs a transaction")
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise ValueError("import needs foreign keys enabled")
    if not isinstance(relay, ImportedRelay):
        raise ValueError("import needs an ImportedRelay")
    if not isinstance(marked_at_ms, int):
        raise ValueError("marked_at_ms must be an int")
    conn.execute("SAVEPOINT engine_import")
    try:
        _validate(conn, project_id, relay, marked_at_ms)
        last_id = conn.execute("SELECT max(id) FROM relays WHERE project_id = ?",
                               (project_id,)).fetchone()[0]
        last_handle = conn.execute("SELECT max(handle) FROM relays WHERE project_id = ?",
                                   (project_id,)).fetchone()[0]
        handle = (last_handle or 0) + 1
        relay_id = submit.new_uuid7(relay.created_at, last_id)
        meta_sha256 = hashlib.sha256(jcs.jcs_encode({
            "import": "2.9.7", "header_sha256": relay.header_sha256,
            "parent": relay.parent_id})).hexdigest()
        conn.execute(
            "INSERT INTO relays (id, handle, project_id, team_instance_id, kind, verdict, needs,"
            " subject, parent_id, sender_seat_id, sender_special, sender_role, created_at,"
            " body_sha256, meta_sha256, body_bytes, header_bytes)"
            " VALUES (?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (relay_id, handle, project_id, relay.team_instance_id, relay.kind, relay.verdict,
             relay.subject, relay.parent_id, *submit.party_columns(relay.sender),
             relay.sender_role, relay.created_at, hashlib.sha256(relay.body).hexdigest(),
             meta_sha256, len(relay.body), relay.header_bytes))
        conn.execute("INSERT INTO bodies (relay_id, body) VALUES (?, ?)", (relay_id, relay.body))
        conn.executemany(
            "INSERT INTO recipients (relay_id, seat_id, special, role, delivered_at, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(relay_id, *submit.party_columns(party), role,
              marked_at_ms if role == "to" else None, marked_at_ms)
             for role, parties in (("to", relay.to), ("cc", relay.cc)) for party in parties])
        if relay.parent_id is not None:
            conn.execute(
                "INSERT INTO edges (id, from_relay_id, ordinal, kind, target_type, spelling,"
                " target_relay_id) VALUES (?, ?, 1, 'parent', 'relay', ?, ?)",
                (submit.new_uuid7(relay.created_at), relay_id, relay.parent_spelling,
                 relay.parent_id))
        conn.execute("RELEASE engine_import")
        return relay_id
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO engine_import")
            conn.execute("RELEASE engine_import")
        raise


def _validate(conn, project_id, relay, marked_at_ms):
    """D2's ordered contract checks, before the first write."""
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise ValueError("the import project does not exist")
    if not isinstance(relay.kind, str) or relay.kind not in header.KINDS:
        raise ValueError("the import kind is not supported")
    if relay.verdict is not None and (not isinstance(relay.verdict, str)
            or relay.kind not in header.VERDICTS or relay.verdict not in header.VERDICTS[relay.kind]):
        raise ValueError("the import verdict does not belong to its kind")
    if relay.sender == "operator":
        valid_sender = relay.sender_role is None and relay.team_instance_id is None
    else:
        seat = conn.execute(
            "SELECT s.role, s.team_instance_id FROM seats s JOIN team_instances t"
            " ON t.id = s.team_instance_id WHERE s.id = ? AND t.project_id = ?",
            (relay.sender, project_id)).fetchone()
        valid_sender = seat is not None and seat == (relay.sender_role, relay.team_instance_id)
    if not valid_sender:
        raise ValueError("the import sender does not match its project, role and team")
    parties = relay.to + relay.cc if isinstance(relay.to, tuple) and isinstance(relay.cc, tuple) else None
    if (parties is None or any(not isinstance(party, str) for party in parties)
            or len(set(parties)) != len(parties) or any(
                party != "operator" and conn.execute(
                    "SELECT 1 FROM seats s JOIN team_instances t ON t.id = s.team_instance_id"
                    " WHERE s.id = ? AND t.project_id = ?", (party, project_id)).fetchone() is None
                for party in parties)):
        raise ValueError("the import parties must be unique and belong to the project")
    if relay.parent_id is None:
        valid_parent = relay.parent_spelling is None
    else:
        valid_parent = (isinstance(relay.parent_spelling, str) and bool(relay.parent_spelling)
                        and conn.execute("SELECT 1 FROM relays WHERE id = ? AND project_id = ?",
                            (relay.parent_id, project_id)).fetchone() is not None)
    if not valid_parent:
        raise ValueError("the import parent and spelling must name this project's relay")
    newest = conn.execute("SELECT max(created_at) FROM relays WHERE project_id = ?",
                          (project_id,)).fetchone()[0]
    if (not isinstance(relay.created_at, int) or not 0 <= relay.created_at < 2**48
            or (newest is not None and not relay.created_at > newest)
            or marked_at_ms < relay.created_at):
        raise ValueError("the import time must follow the project and precede its marker")
    if (not isinstance(relay.header_bytes, int) or relay.header_bytes < 0
            or not isinstance(relay.header_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", relay.header_sha256) is None
            or not isinstance(relay.body, bytes) or not isinstance(relay.subject, str)):
        raise ValueError("the import header, body or subject is invalid")
