"""Reconciliation, verification, rendering, and reader views."""

import hashlib
import json
import os
import re

from relay_engine import errors, migrate
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.jcs import jcs_encode
from relay_engine.ledger import epoch_state
from relay_engine.render import (index_rows, render_index, render_relay,
                                 render_seats, seats_rows, sweep_tmp,
                                 _record_events)


_RELAY_NAME = re.compile(
    r"^[A-Z][A-Z-]*-[A-Za-z0-9-]+-(\d{8}-\d{6}Z?)\.md$")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def admissible_paths(root):
    found = []
    inventoried = []
    for directory, names, files in os.walk(root.path):
        names[:] = [name for name in names if name != ".engine"]
        for name in files:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root.path)
            if rel in {"INDEX.md", "SEATS.md"} or name.startswith(
                    ".relay-tmp-"):
                continue
            if "/" in rel and _RELAY_NAME.fullmatch(name):
                found.append(rel)
            else:
                inventoried.append(rel)
    return sorted(found), sorted(inventoried)


def reconcile(ledger, root):
    paths, inventoried = admissible_paths(root)
    if epoch_state(ledger) != "active":
        return {"epoch": "inert", "ingested": [],
                "inventoried": inventoried + paths,
                "hand": _hand_entries(ledger)}
    candidates, malformed = prepare_candidates(ledger, root, paths)
    ingested = ingest_candidates(ledger, candidates)
    return {"epoch": "active", "ingested": ingested,
            "inventoried": inventoried, "malformed": malformed,
            "hand": _hand_entries(ledger)}


def _hand_entries(ledger):
    return [
        {"path": path, "origin": "hand",
         "advisories": json.loads(advisories)}
        for path, advisories in ledger.execute(
            "SELECT rendered_path,advisories_json FROM relays "
            "WHERE origin='hand' ORDER BY seq")
    ]


def prepare_candidates(ledger, root, paths):
    candidates = []
    malformed = []
    for rel in paths:
        if ledger.execute(
                "SELECT 1 FROM relays WHERE rendered_path=?", (rel,)
        ).fetchone() is not None:
            continue
        try:
            body = root.open_read(rel)
            envelope = parse_draft(body.decode("utf-8"))
            match = _RELAY_NAME.fullmatch(rel.rsplit("/", 1)[-1])
            stamp = match.group(1).rstrip("Z")
            candidates.append((rel, stamp, body, envelope))
        except (OSError, UnicodeDecodeError, ValueError,
                errors.EngineError):
            malformed.append(rel)
    return candidates, malformed


def ingest_candidates(ledger, candidates, *, own_transaction=True):
    """Insert a prepared path-ordered hand batch atomically."""
    ingested = []
    if own_transaction:
        ledger.execute("BEGIN IMMEDIATE")
    try:
        for rel, stamp, body, envelope in candidates:
            seq = ledger.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM relays").fetchone()[0]
            ledger.execute(
                "INSERT INTO relays("
                "seq,submission_id,stamp,rendered_path,admits_against_seq,"
                "phase,role,dispatch_id,parent_dispatch_id,run_id,from_seat,"
                "to_seats,cc_seats,status,subject,headers_json,body,"
                "body_sha256,content_hash,origin,occupancy_ref,advisories_json"
                ") VALUES(?,NULL,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "'hand',NULL,?)",
                (seq, stamp, rel, envelope.phase, envelope.role,
                 envelope.dispatch_id, envelope.parent_dispatch_id,
                 envelope.run_id, envelope.from_seat,
                 jcs_encode(list(envelope.to_seats)).decode("utf-8"),
                 jcs_encode(list(envelope.cc_seats)).decode("utf-8"),
                 envelope.status, envelope.subject,
                 jcs_encode(envelope.headers).decode("utf-8"), body,
                 body_sha256(body), content_hash(envelope, body, None),
                 jcs_encode(["hand-authored-import"]).decode("utf-8")))
            ingested.append(rel)
    except BaseException:
        if own_transaction:
            ledger.execute("ROLLBACK")
        raise
    else:
        if own_transaction:
            ledger.execute("COMMIT")
    return ingested


def projection_entries(ledger):
    rows = ledger.execute(
        "SELECT p.target,p.event,p.digest,p.detail,r.rendered_path "
        "FROM projection_events p LEFT JOIN relays r ON r.seq=p.relay_seq "
        "ORDER BY p.seq").fetchall()
    entries = []
    for target, event, digest, detail, relay_path in rows:
        if target == "relay":
            path = relay_path
        elif target == "index":
            path = "INDEX.md"
        elif target == "seats":
            path = "SEATS.md"
        else:
            path = detail
        entries.append({"target": target, "event": event,
                        "path": path, "digest": digest})
    return entries


def verify(ledger, root):
    dispositions = []
    for seq, rel, expected in ledger.execute(
            "SELECT seq,rendered_path,body_sha256 FROM relays "
            "WHERE origin!='adopted' ORDER BY seq"):
        try:
            actual = root.open_read(rel)
        except FileNotFoundError:
            _record_events(ledger, [(seq, "relay", "missing", None, rel)])
            render_relay(ledger, root, seq)
            dispositions.append({"target": "relay", "event": "missing",
                                 "path": rel, "digest": None})
            continue
        digest = _digest(actual)
        event = "rendered" if digest == expected else "modified"
        if event == "modified":
            _record_events(ledger, [(seq, "relay", event, digest, rel)])
        dispositions.append({"target": "relay", "event": event,
                             "path": rel, "digest": digest})
    known = {(entry["target"], entry["event"], entry["path"],
              entry["digest"]) for entry in dispositions}
    for entry in projection_entries(ledger):
        identity = (entry["target"], entry["event"], entry["path"],
                    entry["digest"])
        if identity not in known:
            dispositions.append(entry)
    return {"ok": not any(entry["event"] in {
                "modified", "conflict", "divergence", "failed"}
            for entry in dispositions), "dispositions": dispositions}


def render_all(ledger, root):
    epoch = epoch_state(ledger)
    if epoch != "active":
        return {"rendered": [], "epoch": epoch}
    rendered = []
    for seq, in ledger.execute(
            "SELECT seq FROM relays WHERE origin!='adopted' ORDER BY seq"):
        result = render_relay(ledger, root, seq)
        rendered.append({"seq": seq, "event": result.event})
    render_index(root, index_rows(ledger), 10, epoch)
    render_seats(root, seats_rows(ledger), epoch)
    return {"rendered": rendered, "epoch": epoch}


def show(ledger, target):
    try:
        seq = int(target)
    except (TypeError, ValueError):
        row = ledger.execute(
            "SELECT seq,rendered_path,origin,body_sha256,content_hash,body "
            "FROM relays WHERE rendered_path=?", (target,)).fetchone()
    else:
        row = ledger.execute(
            "SELECT seq,rendered_path,origin,body_sha256,content_hash,body "
            "FROM relays WHERE seq=?", (seq,)).fetchone()
    if row is None:
        raise errors.error_for("E-ENVELOPE")
    event = ledger.execute(
        "SELECT event FROM projection_events WHERE relay_seq=? "
        "ORDER BY seq DESC LIMIT 1", (row[0],)).fetchone()
    return {"seq": row[0], "path": row[1], "origin": row[2],
            "body_sha256": row[3], "content_hash": row[4],
            "render_state": "render-pending" if event is None else event[0],
            "body": bytes(row[5])}


def status(ledger, daemon_state):
    pending = ledger.execute(
        "SELECT COUNT(*) FROM relays r WHERE NOT EXISTS("
        "SELECT 1 FROM projection_events p WHERE p.relay_seq=r.seq "
        "AND p.event IN ('rendered','repaired'))").fetchone()[0]
    entries = projection_entries(ledger)
    return {"daemon": dict(daemon_state), "epoch": epoch_state(ledger),
            "pending_renders": pending,
            "conflicts": sum(entry["event"] in {
                "conflict", "divergence", "modified"} for entry in entries),
            "projection_events": entries}


def startup_recovery(context):
    ledger = context["ledger"]
    root = context["root"]
    sweep_tmp(root)
    epoch_state(ledger)
    migrate.complete_pending_rollback(ledger, root)
    reconcile(ledger, root)
    if epoch_state(ledger) == "active":
        verify(ledger, root)
        render_all(ledger, root)
