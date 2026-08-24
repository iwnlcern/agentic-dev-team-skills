"""Parent commissioning records and child run-identity adoption."""

import base64
import os
import stat

from relay_engine import errors
from relay_engine.envelope import valid_run_id
from relay_engine.jcs import frame_record, parse_record
from relay_engine.ledger import establish_run_identity
from relay_engine.paths import TempWrite


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _record(ledger, dispatch_path, child_run_id):
    if not valid_run_id(child_run_id):
        raise errors.error_for("run-id-invalid")
    row = ledger.execute(
        "SELECT seq,content_hash FROM relays WHERE rendered_path=?",
        (dispatch_path,)).fetchone()
    if row is None:
        raise errors.error_for("E-ENVELOPE")
    root_uuid = ledger.execute(
        "SELECT value FROM meta WHERE key='root_uuid'").fetchone()[0]
    body = frame_record({
        "v": 1, "parent_root_uuid": root_uuid,
        "commissioning_path": dispatch_path,
        "dispatch_content_digest": row[1],
        "child_run_id": child_run_id,
    })
    _, digest = parse_record(body)
    return row[0], row[1], digest, body


def _exports_fd(root):
    engine_fd = os.open(".engine", _DIR_FLAGS, dir_fd=root.dirfd)
    try:
        try:
            os.mkdir("exports", 0o700, dir_fd=engine_fd)
            os.fsync(engine_fd)
        except FileExistsError:
            pass
        fd = os.open("exports", _DIR_FLAGS, dir_fd=engine_fd)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise NotADirectoryError("exports")
        return fd
    finally:
        os.close(engine_fd)


def _materialize(root, child_run_id, body):
    name = "commission-%s.record" % child_run_id
    exports_fd = _exports_fd(root)
    try:
        with TempWrite._from_parent(exports_fd, name) as pending:
            pending.write(body)
            pending.rename_replace()
    finally:
        os.close(exports_fd)
    return ".engine/exports/" + name


def commission(ledger, root, dispatch_path, child_run_id):
    dispatch_seq, content_digest, record_digest, body = _record(
        ledger, dispatch_path, child_run_id)
    ledger.execute("BEGIN IMMEDIATE")
    try:
        existing = ledger.execute(
            "SELECT dispatch_seq,dispatch_content_digest,record_digest "
            "FROM commissions WHERE child_run_id=?", (child_run_id,)
        ).fetchone()
        if existing is None:
            ledger.execute(
                "INSERT INTO commissions("
                "child_run_id,dispatch_seq,dispatch_content_digest,record_digest"
                ") VALUES(?,?,?,?)",
                (child_run_id, dispatch_seq, content_digest, record_digest))
        elif existing != (dispatch_seq, content_digest, record_digest):
            raise errors.error_for("commission-conflict")
    except BaseException:
        ledger.execute("ROLLBACK")
        raise
    else:
        ledger.execute("COMMIT")
    path = _materialize(root, child_run_id, body)
    return {"record_b64": base64.b64encode(body).decode("ascii"),
            "record_path": path}


def adopt(ledger, record):
    if not isinstance(record, bytes):
        raise TypeError("commissioning record bytes required")
    value = establish_run_identity(ledger, None, record)
    return {"commissioned_by": value}


def startup_run_identity(context):
    establish_run_identity(
        context["ledger"], context.get("run_id"),
        context.get("commissioning_record"))
