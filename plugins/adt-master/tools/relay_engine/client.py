"""Synchronous client operations over the daemon's framed local socket."""

import base64
import json
import os
from pathlib import Path
import socket
import stat
import uuid

from relay_engine import errors, strings, version
from relay_engine.daemon import FrameDecoder, encode_frame
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.paths import Root, clear_sid, sid_for


class RemoteError(Exception):
    def __init__(self, value):
        self.code = value["code"]
        self.cause = value["cause"]
        self.remedy = value["remedy"]
        self.cls = value["cls"]
        super().__init__(self.code)


V1_ADMIN_OPS = {"status", "daemon.stop"}
_FP_ERRORS = {"missing-member", "extra-member", "non-regular-member",
              "unreadable-member"}


def discover_root(start=None):
    current = os.path.realpath(os.path.abspath(
        os.getcwd() if start is None else os.fspath(start)))
    while True:
        engine = os.path.join(current, ".engine")
        try:
            info = os.lstat(engine)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISDIR(info.st_mode):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            raise FileNotFoundError("relay root not found")
        current = parent


def _relative(root, path):
    if not os.path.isabs(path):
        return path
    canonical = os.path.realpath(path)
    prefix = root.path + os.sep
    if not canonical.startswith(prefix):
        raise errors.error_for("E-PATH-ESCAPE")
    return canonical[len(prefix):]


def _open_required(root, rel, field):
    try:
        return root.open_read(rel)
    except FileNotFoundError as exc:
        raise errors.error_for("E-PATH-ESCAPE", variant="not-found",
                               field=field, rel=rel) from exc


def _local_identity(tools_dir=None):
    if tools_dir is None:
        tools_dir = Path(__file__).resolve().parents[1]
    tools_dir = Path(tools_dir).resolve()
    identity = {"kit": version.KIT_VERSION, "install": os.fspath(tools_dir)}
    try:
        identity["fp"] = version.fingerprint(tools_dir)
    except version.FingerprintError as error:
        identity["fp_error"] = str(error)
    return identity


def _valid_identity(value):
    if not isinstance(value, dict):
        return False
    if set(value) == {"kit", "fp", "install"}:
        fingerprint_valid = strings.valid_fp(value["fp"])
    elif set(value) == {"kit", "fp_error", "install"}:
        fingerprint_valid = (isinstance(value["fp_error"], str) and
                             value["fp_error"] in _FP_ERRORS)
    else:
        return False
    return (strings.valid_kit(value["kit"]) and fingerprint_valid and
            strings.valid_install(value["install"]))


def _matching_identity(cid, did):
    return (_valid_identity(cid) and _valid_identity(did) and
            "fp" in cid and "fp" in did and
            cid["kit"] == did["kit"] and cid["fp"] == did["fp"])


def _same_identity(first, second):
    if not _valid_identity(first) or not _valid_identity(second):
        return False
    if first["kit"] != second["kit"]:
        return False
    if "fp" in first and "fp" in second:
        return first["fp"] == second["fp"]
    if "fp_error" in first and "fp_error" in second:
        return first["fp_error"] == second["fp_error"]
    return False


def _identity_value(identity, key):
    return identity.get(key) if isinstance(identity, dict) else None


def _mismatch(cid, did, *, old_daemon=False):
    if old_daemon:
        did = {
            "kit": "0.0.0", "fp": "0" * 64,
            "install": _identity_value(did, "socket"),
        }
    error = errors.error_for(
        "E-VERSION-MISMATCH",
        client_install=_identity_value(cid, "install"),
        client_kit=_identity_value(cid, "kit"),
        client_fp=_identity_value(cid, "fp"),
        daemon_install=_identity_value(did, "install"),
        daemon_kit=_identity_value(did, "kit"),
        daemon_fp=_identity_value(did, "fp"),
    )
    client_damaged = isinstance(cid, dict) and "fp_error" in cid
    daemon_damaged = isinstance(did, dict) and "fp_error" in did
    if not old_daemon and client_damaged != daemon_damaged:
        stale = "client" if client_damaged else "daemon"
        remedy = errors._VersionMismatchRemedy(
            stale, error.params["client_install"],
            error.params["daemon_install"])
        error.params["remedy"] = remedy
        error.remedy = strings.render(
            "error-version-mismatch-remedy", remedy=remedy)
    return RemoteError(error.as_dict())


def _daemon_record(root_name):
    try:
        with Root(root_name) as root:
            record = json.loads(root.open_read(".engine/daemon.json"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict()) from exc
    if (not isinstance(record, dict) or record.get("state") != "ready" or
            not isinstance(record.get("socket"), str)):
        raise RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict())
    return record


def _roundtrip(socket_name, request_value, timeout=10.0):
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(timeout)
    try:
        connection.connect(socket_name)
        connection.sendall(encode_frame(request_value))
        decoder = FrameDecoder()
        while True:
            data = connection.recv(65536)
            if not data:
                raise ConnectionError("daemon closed before response")
            frames = decoder.feed(data)
            if frames:
                return json.loads(frames[0].decode("utf-8"))
    finally:
        connection.close()


def request(root_name, op, args, timeout=10.0, cid=None):
    if cid is None:
        cid = _local_identity()
    else:
        cid = dict(cid) if isinstance(cid, dict) else cid
    record = _daemon_record(root_name)
    identity_present = "identity" in record
    old_daemon = (not identity_present and
                  type(record.get("version")) is int and
                  record["version"] == 1)
    if not identity_present and not old_daemon:
        raise _mismatch(cid, record)
    if old_daemon:
        if op not in V1_ADMIN_OPS:
            raise _mismatch(cid, record, old_daemon=True)
        protocol = 1
    else:
        protocol = 2
        daemon_identity = record["identity"]
        if not _valid_identity(cid) or not _valid_identity(daemon_identity):
            raise _mismatch(cid, daemon_identity)
        if op not in V1_ADMIN_OPS and not _matching_identity(
                cid, daemon_identity):
            raise _mismatch(cid, record["identity"])
    request_id = str(uuid.uuid4())
    value = {"v": protocol, "id": request_id, "op": op, "args": args}
    if protocol == 2:
        value["cid"] = cid
    try:
        response = _roundtrip(record["socket"], value, timeout)
    except TimeoutError:
        raise
    except (ConnectionError, FileNotFoundError, OSError) as exc:
        raise RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict()) from exc
    if response.get("id") != request_id:
        raise ValueError("response id mismatch")
    if response.get("v") != protocol:
        raise ValueError("response version mismatch")
    if protocol == 2:
        response_did = response.get("did")
        if not _same_identity(record["identity"], response_did):
            raise _mismatch(cid, response_did)
    if not response.get("ok"):
        raise RemoteError(response["error"])
    return response["result"]


def submit(root_name, draft, key_path=None, admits_against=None,
           timeout=10.0, cid=None):
    with Root(root_name) as root:
        draft_rel = _relative(root, draft)
        if key_path is None:
            key_path = os.environ.get("RELAY_KEY")
        if not key_path:
            raise errors.error_for("E-KEY-MISMATCH")
        key_rel = _relative(root, key_path)
        body = _open_required(root, draft_rel, "draft")
        try:
            envelope = parse_draft(body.decode("utf-8"))
            tag = _open_required(root, key_rel, "key").decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise errors.error_for("E-ENVELOPE") from exc
        submission_id = sid_for(root, draft_rel)
        args = {
            "envelope": envelope.headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "body_sha256": body_sha256(body),
            "content_hash": content_hash(envelope, body, admits_against),
            "submission_id": submission_id,
            "tag": tag,
        }
        if admits_against is not None:
            args["admits_against"] = admits_against
    result = request(root_name, "submit", args, timeout=timeout, cid=cid)
    with Root(root_name) as root:
        clear_sid(root, draft_rel)
    return result


def seat_register(root_name, address, role_word, dispatch_ref=None,
                  replace=False, timeout=10.0, cid=None):
    args = {"address": address, "role_word": role_word}
    if dispatch_ref is not None:
        args["dispatch_ref"] = dispatch_ref
    if replace:
        args["replace"] = True
    op = "seat.replace" if replace else "seat.register"
    return request(root_name, op, args, timeout=timeout, cid=cid)


def seat_stand_down(root_name, address, timeout=10.0, cid=None):
    return request(root_name, "seat.stand_down", {"address": address},
                   timeout=timeout, cid=cid)


def seat_show(root_name, address, timeout=10.0, cid=None):
    return request(root_name, "seat.show", {"address": address},
                   timeout=timeout, cid=cid)


def commission_run(root_name, dispatch_path, child_run_id, timeout=10.0,
                   cid=None):
    return request(root_name, "commission", {
        "dispatch_relay_path": dispatch_path,
        "child_run_id": child_run_id,
    }, timeout=timeout, cid=cid)


def adopt_commission(root_name, record, timeout=10.0, cid=None):
    if not isinstance(record, bytes):
        raise TypeError("record bytes required")
    return request(root_name, "adopt_commission", {
        "record_b64": base64.b64encode(record).decode("ascii"),
    }, timeout=timeout, cid=cid)


def export_ruling(root_name, ruling_path, child_run_id, timeout=10.0,
                  cid=None):
    return request(root_name, "export_ruling", {
        "ruling_path": ruling_path, "for_child": child_run_id,
    }, timeout=timeout, cid=cid)


def adopt_ruling(root_name, bundle, timeout=10.0, cid=None):
    if not isinstance(bundle, bytes):
        raise TypeError("bundle bytes required")
    return request(root_name, "adopt_ruling", {
        "bundle_b64": base64.b64encode(bundle).decode("ascii"),
    }, timeout=timeout, cid=cid)
