"""Synchronous client operations over the daemon's framed local socket."""

import base64
import json
import os
import socket
import stat
import uuid

from relay_engine import errors
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


def _daemon_socket(root_name):
    try:
        with Root(root_name) as root:
            record = json.loads(root.open_read(".engine/daemon.json"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict()) from exc
    if record.get("state") != "ready" or not isinstance(
            record.get("socket"), str):
        raise RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict())
    return record["socket"]


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


def request(root_name, op, args, timeout=10.0):
    request_id = str(uuid.uuid4())
    value = {"v": 1, "id": request_id, "op": op, "args": args}
    try:
        response = _roundtrip(_daemon_socket(root_name), value, timeout)
    except TimeoutError:
        raise
    except (ConnectionError, FileNotFoundError, OSError) as exc:
        raise RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict()) from exc
    if response.get("id") != request_id:
        raise ValueError("response id mismatch")
    if not response.get("ok"):
        raise RemoteError(response["error"])
    return response["result"]


def submit(root_name, draft, key_path=None, admits_against=None,
           timeout=10.0):
    with Root(root_name) as root:
        draft_rel = _relative(root, draft)
        if key_path is None:
            key_path = os.environ.get("RELAY_KEY")
        if not key_path:
            raise errors.error_for("E-KEY-MISMATCH")
        key_rel = _relative(root, key_path)
        body = root.open_read(draft_rel)
        try:
            envelope = parse_draft(body.decode("utf-8"))
            tag = root.open_read(key_rel).decode("utf-8").strip()
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
    result = request(root_name, "submit", args, timeout=timeout)
    with Root(root_name) as root:
        clear_sid(root, draft_rel)
    return result


def seat_register(root_name, address, role_word, dispatch_ref=None,
                  replace=False, timeout=10.0):
    args = {"address": address, "role_word": role_word}
    if dispatch_ref is not None:
        args["dispatch_ref"] = dispatch_ref
    if replace:
        args["replace"] = True
    op = "seat.replace" if replace else "seat.register"
    return request(root_name, op, args, timeout=timeout)


def seat_stand_down(root_name, address, timeout=10.0):
    return request(root_name, "seat.stand_down", {"address": address},
                   timeout=timeout)


def seat_show(root_name, address, timeout=10.0):
    return request(root_name, "seat.show", {"address": address},
                   timeout=timeout)


def commission_run(root_name, dispatch_path, child_run_id, timeout=10.0):
    return request(root_name, "commission", {
        "dispatch_relay_path": dispatch_path,
        "child_run_id": child_run_id,
    }, timeout=timeout)


def adopt_commission(root_name, record, timeout=10.0):
    if not isinstance(record, bytes):
        raise TypeError("record bytes required")
    return request(root_name, "adopt_commission", {
        "record_b64": base64.b64encode(record).decode("ascii"),
    }, timeout=timeout)


def export_ruling(root_name, ruling_path, child_run_id, timeout=10.0):
    return request(root_name, "export_ruling", {
        "ruling_path": ruling_path, "for_child": child_run_id,
    }, timeout=timeout)


def adopt_ruling(root_name, bundle, timeout=10.0):
    if not isinstance(bundle, bytes):
        raise TypeError("bundle bytes required")
    return request(root_name, "adopt_ruling", {
        "bundle_b64": base64.b64encode(bundle).decode("ascii"),
    }, timeout=timeout)
