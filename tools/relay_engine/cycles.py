"""Dispatch-id scope participation, closure, and reopen derivation."""

import json

from relay_engine import errors


def _opener(ledger, dispatch_id):
    return ledger.execute(
        "SELECT seq,from_seat,to_seats,rendered_path FROM relays "
        "WHERE dispatch_id=? AND origin!='adopted' ORDER BY seq LIMIT 1",
        (dispatch_id,)).fetchone()


def participants(ledger, dispatch_id):
    rows = ledger.execute(
        "SELECT from_seat,to_seats FROM relays WHERE dispatch_id=? "
        "AND origin!='adopted' ORDER BY seq", (dispatch_id,)).fetchall()
    if not rows:
        return set()
    current = {rows[0][0], *json.loads(rows[0][1])}
    for sender, encoded_to in rows[1:]:
        if sender in current:
            current.update(json.loads(encoded_to))
    return current


def state(ledger, dispatch_id):
    row = ledger.execute(
        "SELECT event FROM cycle_events WHERE dispatch_id=? "
        "ORDER BY seq DESC LIMIT 1", (dispatch_id,)).fetchone()
    return "open" if row is None or row[0] in ("open", "reopen") else "closed"


def _edge_seq(ledger, path):
    if path is None:
        return None
    row = ledger.execute(
        "SELECT seq FROM relays WHERE rendered_path=?", (path,)).fetchone()
    return None if row is None else row[0]


def precheck(ledger, envelope, edge_seq):
    opener = _opener(ledger, envelope.dispatch_id)
    if opener is None:
        if edge_seq is not None:
            raise errors.error_for("E-ID-COLLISION")
        return
    if edge_seq is None:
        raise errors.error_for("E-ID-COLLISION")
    edge_dispatch = ledger.execute(
        "SELECT dispatch_id FROM relays WHERE seq=?", (edge_seq,)
    ).fetchone()
    if edge_dispatch != (envelope.dispatch_id,):
        raise errors.error_for("E-ID-COLLISION")
    if envelope.from_seat not in participants(ledger, envelope.dispatch_id):
        raise errors.error_for("E-ID-COLLISION")
    if state(ledger, envelope.dispatch_id) != "open":
        if not (envelope.headers.get("CYCLE_REOPEN") is not None and
                _eligible_control(ledger, envelope)):
            raise errors.error_for("E-ID-COLLISION")
    if ledger.execute(
            "SELECT 1 FROM supersession_edges WHERE target_seq=? "
            "AND applied=1", (edge_seq,)).fetchone() is not None:
        raise errors.error_for("E-SUPERSEDED")


def _eligible_control(ledger, envelope):
    opener = _opener(ledger, envelope.dispatch_id)
    if opener is None:
        return False
    return (envelope.from_seat in {opener[1], "operator"} or
            envelope.from_seat.endswith(".orchestrator-planner"))


def _terminus_effective(ledger, envelope, value):
    if value == "withdrawn":
        return _eligible_control(ledger, envelope)
    if value == "plan-rejected":
        return envelope.phase.upper() == "PLAN-REVIEW"
    if value == "design-rejected":
        return envelope.phase.upper() == "DESIGN-REVIEW"
    if value == "audit-closed":
        return envelope.phase.upper() == "AUDIT" and envelope.status in {
            "already-closed", "product-overlapped"}
    if value == "live-verified":
        return (envelope.phase.upper() == "LIVE-VERIFY" and
                envelope.status == "complete")
    if value == "merged":
        return envelope.status == "merged"
    return False


def prepare(ledger, envelope, admits_against):
    opener = _opener(ledger, envelope.dispatch_id)
    events = []
    advisories = []
    if opener is None:
        events.append({"dispatch_id": envelope.dispatch_id, "event": "open",
                       "commissioning_seq": "current-relay"})
        return events, advisories
    terminus = envelope.headers.get("CYCLE_TERMINUS")
    reopen = envelope.headers.get("CYCLE_REOPEN")
    if terminus is not None:
        if state(ledger, envelope.dispatch_id) == "closed":
            advisories.append("cycle-already-closed")
        elif _terminus_effective(ledger, envelope, terminus):
            events.append({"dispatch_id": envelope.dispatch_id,
                           "event": "close",
                           "cause_seq": "current-relay"})
        else:
            advisories.append("cycle-terminus-ineffective")
    if reopen is not None:
        if state(ledger, envelope.dispatch_id) == "closed" and \
                _eligible_control(ledger, envelope):
            events.append({"dispatch_id": envelope.dispatch_id,
                           "event": "reopen",
                           "cause_seq": "current-relay"})
        else:
            advisories.append("cycle-reopen-ineffective")
    return events, advisories


def callback():
    return precheck
