"""Occupancy-bound seat registration and roster operations."""

import os
import re
import secrets
import stat
import uuid

from relay_engine import errors
from relay_engine.envelope import parse_draft
from relay_engine.ledger import admit, append_seat_events, epoch_state
from relay_engine.paths import TempWrite
from relay_engine.render import (index_rows, render_index, render_relay,
                                 render_seats, seats_rows)


_ADDRESS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROLE = re.compile(r"^[A-Za-z][A-Za-z0-9 -]{0,63}$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _validate(address, role_word=None, dispatch_ref=None):
    if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:
        raise errors.error_for("E-ENVELOPE")
    if role_word is not None and (
            not isinstance(role_word, str) or
            _ROLE.fullmatch(role_word) is None):
        raise errors.error_for("E-ENVELOPE")
    if dispatch_ref is not None and (
            not isinstance(dispatch_ref, str) or not dispatch_ref or
            "/" in dispatch_ref or "\n" in dispatch_ref):
        raise errors.error_for("E-ENVELOPE")


def _latest(ledger, address):
    return ledger.execute(
        "SELECT seq,event,occupant_id,key_id,boot_relay_seq "
        "FROM seat_events WHERE address=? ORDER BY seq DESC LIMIT 1",
        (address,)).fetchone()


def _run_id(ledger):
    row = ledger.execute(
        "SELECT value FROM meta WHERE key='run_id'").fetchone()
    if row is None:
        raise errors.error_for("run-id-uninitialized")
    return row[0]


def _ensure_dir(parent_fd, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise NotADirectoryError(name)
    return fd


def _issue_key(root, address, occupant_id, tag):
    engine_fd = os.open(".engine", _DIR_FLAGS, dir_fd=root.dirfd)
    try:
        seats_fd = _ensure_dir(engine_fd, "seats")
        try:
            address_fd = _ensure_dir(seats_fd, address)
            try:
                name = occupant_id + ".key"
                with TempWrite._from_parent(address_fd, name) as pending:
                    pending.write((tag + "\n").encode("ascii"))
                    pending.rename_noclobber()
            finally:
                os.close(address_fd)
        finally:
            os.close(seats_fd)
    finally:
        os.close(engine_fd)
    return ".engine/seats/%s/%s.key" % (address, occupant_id)


def _remove_issued_key(root, path):
    parts = path.split("/")
    parent = os.open("/".join(parts[:-1]), _DIR_FLAGS, dir_fd=root.dirfd)
    try:
        os.unlink(parts[-1], dir_fd=parent)
        os.fsync(parent)
    except FileNotFoundError:
        pass
    finally:
        os.close(parent)


def _boot_bytes(address, role_word, run_id, dispatch_ref, occupant_id,
                commissioned_by=None):
    commissioned_line = ("" if commissioned_by is None else
                         "COMMISSIONED_BY: %s\n" % commissioned_by)
    return (("## relay\n\n"
            "ROLE: %s\n"
            "PHASE: BOOT\n"
            "AUTHORITY: seat-registration\n"
            "DISPATCH_ID: %s\n"
            "RUN_ID: %s\n"
            "CEREMONY_TIER: large\n"
            "EVIDENCE_TARGET: E2\n"
            "HUMAN_GATE_REQUIRED: no\n"
            "FROM: %s\n"
            "TO: %s\n"
            "SUBJECT: seat boot %s\n"
            "%s\n"
            "occupant %s\n"
            "commissioning %s\n" % (
                role_word, dispatch_ref, run_id, address, address,
                occupant_id, commissioned_line, occupant_id,
                dispatch_ref))).encode("utf-8")


def _event(address, event, occupant_id, key_id, **extra):
    value = {"address": address, "event": event,
             "occupant_id": occupant_id, "key_id": key_id}
    value.update(extra)
    return value


def register(ledger, root, address, role_word, dispatch_ref=None, *,
             replace=False, top=False):
    """Issue one occupancy and atomically bind its boot row when required."""
    _validate(address, role_word, dispatch_ref)
    run_id = _run_id(ledger)
    current = _latest(ledger, address)
    active = current is not None and current[1] == "occupied"
    if active and top:
        return {"occupant_id": current[2],
                "key_path": ".engine/seats/%s/%s.key" % (
                    address, current[2]), "boot_relay": None}
    if active and not replace:
        raise errors.error_for("seat-occupied")
    occupant_id = str(uuid.uuid4())
    key_id = str(uuid.uuid4())
    tag = secrets.token_urlsafe(32)
    key_path = _issue_key(root, address, occupant_id, tag)
    dispatch = dispatch_ref or "%s-seat-%s" % (run_id, occupant_id)
    replacement = []
    if active:
        replacement.append(_event(
            address, "replaced", current[2], current[3],
            boot_relay_seq=current[4], cause_seq=current[0]))
    try:
        if top:
            append_seat_events(ledger, replacement + [_event(
                address, "occupied", occupant_id, key_id,
                boot_relay_seq=None,
                cause_seq=(None if current is None else current[0]))])
            boot_path = None
        else:
            commissioned = ledger.execute(
                "SELECT commissioned_by FROM runs WHERE run_id=?",
                (run_id,)).fetchone()
            body = _boot_bytes(
                address, role_word, run_id, dispatch, occupant_id,
                None if commissioned is None else commissioned[0])
            envelope = parse_draft(body.decode("utf-8"))
            admission = admit(
                ledger, root, envelope, body, occupant_id,
                seat_events=replacement + [_event(
                    address, "occupied", occupant_id, key_id,
                    boot_relay_seq="current-relay",
                    cause_seq=(None if current is None else current[0]))])
            render_relay(ledger, root, admission.seq)
            boot_path = admission.rendered_path
        render_index(root, index_rows(ledger), 10, epoch_state(ledger))
        render_seats(root, seats_rows(ledger), epoch_state(ledger))
    except BaseException:
        _remove_issued_key(root, key_path)
        raise
    return {"occupant_id": occupant_id, "key_path": key_path,
            "boot_relay": boot_path}


def replace(ledger, root, address, role_word, dispatch_ref=None):
    return register(ledger, root, address, role_word, dispatch_ref,
                    replace=True)


def stand_down(ledger, root, address):
    _validate(address)
    current = _latest(ledger, address)
    if current is None or current[1] != "occupied":
        raise errors.error_for("E-KEY-MISMATCH")
    append_seat_events(ledger, [_event(
        address, "stood-down", current[2], current[3],
        boot_relay_seq=current[4], cause_seq=current[0])])
    render_seats(root, seats_rows(ledger), epoch_state(ledger))
    return {"address": address, "state": "stood-down"}


def show(ledger, address):
    _validate(address)
    rows = ledger.execute(
        "SELECT seq,event,occupant_id,key_id,boot_relay_seq,cause_seq,detail "
        "FROM seat_events WHERE address=? ORDER BY seq", (address,)
    ).fetchall()
    return {"address": address, "history": [
        {"seq": row[0], "event": row[1], "occupant_id": row[2],
         "key_id": row[3], "boot_relay_seq": row[4],
         "cause_seq": row[5], "detail": row[6]} for row in rows]}


def roster(ledger, root):
    rows = seats_rows(ledger)
    render_seats(root, rows, epoch_state(ledger))
    return {"path": "SEATS.md", "count": len(rows)}


def tag_is_current(ledger, root, address, tag):
    _validate(address)
    current = _latest(ledger, address)
    if current is None or current[1] != "occupied":
        return False
    path = ".engine/seats/%s/%s.key" % (address, current[2])
    try:
        expected = root.open_read(path).decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(expected, tag)


def startup_top_seat(context):
    address = context.get("top_seat")
    if address is not None:
        register(context["ledger"], context["root"], address,
                 context.get("top_role", "Orchestrator Planner"),
                 context.get("top_dispatch"), top=True)
