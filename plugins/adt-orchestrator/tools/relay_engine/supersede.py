"""Local and receipt-backed adopted supersession derivation."""

import base64
import hashlib
import json

from relay_engine import errors
from relay_engine import strings
from relay_engine.envelope import parse_draft
from relay_engine.jcs import frame_record, parse_record
from relay_engine.ledger import admit


def prepare(ledger, envelope):
    target = envelope.headers.get("SUPERSEDES")
    if target is None:
        return (), ()
    row = ledger.execute(
        "SELECT seq FROM relays WHERE rendered_path=?", (target,)).fetchone()
    eligible = (envelope.from_seat == "operator" or
                envelope.from_seat.endswith(".orchestrator-planner"))
    if row is None or not eligible:
        return (), ("supersession-ineffective",)
    return ({"target_seq": row[0], "ruling_seq": "current-relay",
             "source": "local", "applied": 1},), ()


def _chains_to(ledger, start_seq, target_seq):
    current = start_seq
    seen = set()
    while current is not None and current not in seen:
        if current == target_seq:
            return True
        seen.add(current)
        row = ledger.execute(
            "SELECT admits_against_seq FROM relays WHERE seq=?",
            (current,)).fetchone()
        current = None if row is None else row[0]
    return False


def export_ruling(ledger, ruling_path, child_run_id):
    commission_row = ledger.execute(
        "SELECT c.dispatch_seq,c.dispatch_content_digest,r.rendered_path "
        "FROM commissions c JOIN relays r ON r.seq=c.dispatch_seq "
        "WHERE c.child_run_id=?", (child_run_id,)).fetchone()
    if commission_row is None:
        raise errors.error_for("commission-conflict")
    ruling = ledger.execute(
        "SELECT seq,stamp,rendered_path,body_sha256,body FROM relays "
        "WHERE rendered_path=?", (ruling_path,)).fetchone()
    if ruling is None:
        raise errors.error_for("E-ENVELOPE")
    body = bytes(ruling[4])
    envelope = parse_draft(body.decode("utf-8"))
    if envelope.from_seat != "operator" and not _chains_to(
            ledger, ruling[0], commission_row[0]):
        raise errors.error_for("E-SUPERSEDED")
    root_uuid = ledger.execute(
        "SELECT value FROM meta WHERE key='root_uuid'").fetchone()[0]
    bundle = frame_record({
        "v": 1, "parent_root_uuid": root_uuid,
        "child_run_id": child_run_id,
        "commissioning_path": commission_row[2],
        "dispatch_content_digest": commission_row[1],
        "ruling_seq": ruling[0], "ruling_stamp": ruling[1],
        "ruling_path": ruling[2], "ruling_content_digest": ruling[3],
        "ruling_body_b64": base64.b64encode(body).decode("ascii"),
    })
    return {"bundle_b64": base64.b64encode(bundle).decode("ascii")}


# the fields relay export-ruling writes, each with its exact JSON type (a bool
# is not an int here, and 1.0 is not 1)
_CARRIER_TYPES = {
    "v": int, "parent_root_uuid": str, "child_run_id": str,
    "commissioning_path": str, "dispatch_content_digest": str,
    "ruling_seq": int, "ruling_stamp": str, "ruling_path": str,
    "ruling_content_digest": str, "ruling_body_b64": str,
}


def _carrier_field(field, reason):
    return errors.error_for("E-CARRIER-INVALID", variant="field", field=field, reason=reason)


def adopt_ruling(ledger, root, bundle):
    from relay_engine.envelope import draft_shape, shape_error
    try:
        fields, bundle_digest = parse_record(bundle)
    except (ValueError, RecursionError) as exc:  # framing, digest, JSON, nesting
        raise errors.error_for("E-CARRIER-INVALID") from exc
    if set(fields) != set(_CARRIER_TYPES) or any(
            type(fields[name]) is not kind for name, kind in _CARRIER_TYPES.items()):
        raise errors.error_for("E-CARRIER-FIELD")
    if fields["v"] != 1:
        raise errors.error_for("E-CARRIER-FIELD")
    run_row = ledger.execute(
        "SELECT value FROM meta WHERE key='run_id'").fetchone()
    if run_row is None or run_row[0] != fields["child_run_id"]:
        raise errors.error_for("run-id-mismatch")
    commissioned = ledger.execute(
        "SELECT commissioned_by FROM runs WHERE run_id=?",
        (run_row[0],)).fetchone()
    if commissioned is None:
        raise errors.error_for("commission-conflict")
    stored = json.loads(commissioned[0])
    for name in ("parent_root_uuid", "commissioning_path",
                 "dispatch_content_digest"):
        if stored.get(name) != fields[name]:
            raise errors.error_for("commission-conflict")
    try:
        body = base64.b64decode(fields["ruling_body_b64"], validate=True)
    except ValueError as exc:
        raise _carrier_field("ruling_body_b64", "not base64") from exc
    if hashlib.sha256(body).hexdigest() != fields["ruling_content_digest"]:
        raise _carrier_field("ruling_content_digest", "does not match the carried body")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _carrier_field("ruling_body_b64", "not UTF-8 text") from exc
    shape = draft_shape(text)
    if shape is not None:
        raise shape_error(shape)
    try:
        envelope = parse_draft(text)
    except (ValueError, UnicodeDecodeError, errors.EngineError) as exc:
        raise errors.error_for("E-ENVELOPE") from exc
    target_path = envelope.headers.get("SUPERSEDES")
    if target_path is None:
        raise _carrier_field("ruling_body_b64", "the ruling names no SUPERSEDES target")
    target = ledger.execute(
        "SELECT seq FROM relays WHERE rendered_path=?", (target_path,)
    ).fetchone()
    if target is None:
        shown = (target_path if strings._is_show_text(target_path) and len(target_path) <= 300
                 else errors._rejected_identity(target_path))
        raise errors.error_for("E-CARRIER-INVALID", variant="target", path=shown)
    eligible = (envelope.from_seat == "operator" or
                envelope.from_seat.endswith(".orchestrator-planner"))
    adopted_path = "adopted/%s.md" % bundle_digest
    advisory = () if eligible else ("supersession-ineffective",)
    result = admit(
        ledger, root, envelope, body, bundle_digest, origin="adopted",
        stamp=fields["ruling_stamp"], rendered_path=adopted_path,
        advisories=advisory,
        supersession_edges=({
            "target_seq": target[0], "ruling_seq": "current-relay",
            "source": "adopted", "applied": 1 if eligible else 0,
        },))
    return {"ruling_seq": result.seq, "target_seq": target[0],
            "applied": eligible, "duplicate": result.replay}
