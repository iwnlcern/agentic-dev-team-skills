"""Daemon ownership primitives, framed wire validation, and writer queue."""

from concurrent.futures import Future
from dataclasses import dataclass
import errno
import fcntl
import base64
import hashlib
import io
import json
import os
import queue
import re
import select
import socket
import stat
import threading
import time
import uuid

from relay_engine import (commission, cycles, errors, reconcile, rules, seats,
                          strings, supersede)
from relay_engine.ledger import init_schema, open_ledger
from relay_engine.ledger import admit, epoch_state
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.render import index_rows, render_index, render_relay
from relay_engine.paths import Root, TempWrite, ensure_engine_dir


MAX_FRAME = 16 * 1024 * 1024
_STOP = object()
_diag_clock = time.time
_diag_lock = threading.Lock()
_log_fd = None
_PROCESS_STARTED = str(time.time_ns())
_STARTUP_STAGES = {"run-identity": commission.startup_run_identity,
                   "top-seat": seats.startup_top_seat,
                   "recovery": reconcile.startup_recovery}


class WireFault(Exception):
    def __init__(self, error):
        if not isinstance(error, errors.EngineError):
            raise TypeError("EngineError required")
        self.error = error
        super().__init__(error.code)


def encode_frame(value):
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("frame exceeds maximum")
    return len(body).to_bytes(4, "big") + body


class FrameDecoder:
    def __init__(self):
        self._buffer = bytearray()
        self._expected = None

    def feed(self, data):
        if not isinstance(data, bytes):
            raise TypeError("frame bytes required")
        self._buffer.extend(data)
        frames = []
        while True:
            if self._expected is None:
                if len(self._buffer) < 4:
                    break
                self._expected = int.from_bytes(self._buffer[:4], "big")
                del self._buffer[:4]
                if self._expected > MAX_FRAME:
                    self._buffer.clear()
                    self._expected = None
                    raise WireFault(errors.error_for(
                        "E-FRAMING", reason="oversize"))
            if len(self._buffer) < self._expected:
                break
            frames.append(bytes(self._buffer[:self._expected]))
            del self._buffer[:self._expected]
            self._expected = None
        return frames

    def finish(self):
        if self._expected is not None or self._buffer:
            self._buffer.clear()
            self._expected = None
            raise WireFault(errors.error_for(
                "E-FRAMING", reason="truncated"))
        return []


def _rejected(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        raw = type(value).__name__.encode("ascii", "replace")
    return strings.rejected_value(hashlib.sha256(raw).hexdigest()[:12],
                                  len(raw))


_SCHEMAS = {
    "submit": ({"envelope", "body_b64", "body_sha256", "content_hash",
                "submission_id", "tag"}, {"admits_against"}),
    "seat.register": ({"address", "role_word"},
                      {"dispatch_ref", "replace"}),
    "seat.replace": ({"address", "role_word"},
                     {"dispatch_ref", "replace"}),
    "seat.stand_down": ({"address"}, set()),
    "seat.show": ({"address"}, set()),
    "show": ({"target"}, {"body"}),
    "status": (set(), set()),
    "roster": (set(), set()),
    "commission": ({"dispatch_relay_path", "child_run_id"}, set()),
    "adopt_commission": ({"record_b64"}, set()),
    "export_ruling": ({"ruling_path", "for_child"}, set()),
    "adopt_ruling": ({"bundle_b64"}, set()),
    "render": (set(), set()),
    "verify": (set(), set()),
    "reconcile": (set(), set()),
    "migrate.check": (set(), set()),
    "daemon.stop": (set(), set()),
}


def _args_fault(op, detail):
    return WireFault(errors.error_for("E-WIRE-ARGS", op=op,
                                      detail=detail))


def _validate_args(op, args):
    if not isinstance(args, dict):
        raise _args_fault(op, "args:object")
    required, optional = _SCHEMAS[op]
    keys = set(args)
    if not required <= keys or not keys <= required | optional:
        raise _args_fault(op, "args:shape")
    for key, value in args.items():
        if key == "envelope":
            if not isinstance(value, dict):
                raise _args_fault(op, "envelope:object")
        elif key in {"replace", "body"}:
            if not isinstance(value, bool):
                raise _args_fault(op, "replace:boolean")
        elif not isinstance(value, str):
            raise _args_fault(op, "%s:string" % key)


def decode_request(body):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WireFault(errors.error_for(
            "E-FRAMING", reason="not-utf8")) from exc
    try:
        request = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WireFault(errors.error_for(
            "E-FRAMING", reason="not-json")) from exc
    if not isinstance(request, dict):
        raise WireFault(errors.error_for("E-FRAMING", reason="not-json"))
    if set(request) != {"v", "id", "op", "args"}:
        raise WireFault(errors.error_for("E-FRAMING", reason="not-json"))
    request_id = request["id"]
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise WireFault(errors.error_for(
            "E-FRAMING", reason="not-json")) from exc
    if request["v"] != 1:
        raise WireFault(errors.error_for(
            "E-WIRE-VERSION", rejected_version=_rejected(request["v"])))
    op = request["op"]
    if op not in _SCHEMAS:
        raise WireFault(errors.error_for(
            "E-WIRE-OP", rejected_op=_rejected(op)))
    _validate_args(op, request["args"])
    return request


def error_response(request_id, error):
    return {"v": 1, "id": request_id, "ok": False,
            "error": error.as_dict()}


def success_response(request_id, result):
    return {"v": 1, "id": request_id, "ok": True, "result": result}


class SerialWriter:
    def __init__(self, handler):
        self._handler = handler
        self._queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._stopping = False
        self._thread = threading.Thread(target=self._run,
                                        name="relay-writer", daemon=True)

    def start(self):
        self._thread.start()

    def enqueue(self, request):
        with self._state_lock:
            if self._stopping:
                raise errors.error_for("E-DAEMON-STOPPING")
            future = Future()
            self._queue.put((request, future))
            return future

    def begin_stop(self):
        with self._state_lock:
            if not self._stopping:
                self._stopping = True
                self._queue.put(_STOP)

    def _run(self):
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            request, future = item
            try:
                future.set_result(self._handler(request))
            except BaseException as exc:
                future.set_exception(exc)

    def join(self, timeout=None):
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _escape_diagnostic(data):
    output = bytearray()
    for byte in data:
        if byte == 0x5c:
            output.extend(b"\\\\")
        elif byte == 0x0a:
            output.extend(b"\\n")
        elif byte == 0x0d:
            output.extend(b"\\r")
        elif byte < 0x20 or byte == 0x7f:
            output.extend(("\\x%02x" % byte).encode("ascii"))
        else:
            output.append(byte)
    return bytes(output)


def _log_diagnostic(exc):
    if _log_fd is None:
        return
    timestamp_bytes = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(_diag_clock())).encode("ascii")
    escaped = _escape_diagnostic(repr(exc).encode("utf-8"))
    record = b"diag " + timestamp_bytes + b" " + escaped + b"\n"
    with _diag_lock:
        remaining = memoryview(record)
        while remaining:
            try:
                written = os.write(_log_fd, remaining)
            except InterruptedError:
                continue
            if written == 0:
                return
            remaining = remaining[written:]


def start(root_path=None, ready_fd=None, handlers=None, trace=None,
          socket_override=None, top_seat=None, top_role=None,
          top_dispatch=None, run_id=None, commissioning_record=None):
    """Bind diagnostics, or run a complete daemon when a root is supplied."""
    strings.set_diagnostic_sink(_log_diagnostic)
    if root_path is None:
        return None
    return _run_daemon(root_path, ready_fd, handlers or {}, trace,
                       socket_override, top_seat, top_role, top_dispatch,
                       run_id, commissioning_record)


@dataclass
class DaemonLease:
    engine_dirfd: int
    lock_fd: int

    def close(self):
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.engine_dirfd is not None:
            os.close(self.engine_dirfd)
            self.engine_dirfd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def acquire_lease(root):
    if not isinstance(root, Root) or root.dirfd is None:
        raise TypeError("open Root required")
    engine_dirfd = ensure_engine_dir(root.dirfd)
    try:
        lock_fd = os.open(
            "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600, dir_fd=engine_dirfd)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise OSError(errno.EINVAL, "regular lock file required")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(lock_fd)
            raise
        return DaemonLease(engine_dirfd, lock_fd)
    except BaseException:
        os.close(engine_dirfd)
        raise


def open_log(engine_dirfd):
    flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW |
             os.O_NONBLOCK)
    fd = os.open("daemon.log", flags, 0o600, dir_fd=engine_dirfd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError(errno.EPERM, "daemon log boundary mismatch")
        current = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, current & ~os.O_NONBLOCK)
        final = fcntl.fcntl(fd, fcntl.F_GETFL)
        if not final & os.O_APPEND or final & os.O_NONBLOCK:
            raise OSError(errno.EIO, "daemon log flag mismatch")
        return fd
    except BaseException:
        os.close(fd)
        raise


def socket_path(canonical_root, override=None):
    if override is None:
        override = os.environ.get("RELAY_SOCKET")
    if override:
        return override
    digest = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()[:12]
    return "/tmp/relay-engine/%s/s" % digest


def prepare_socket(path, expected_uid=None):
    if expected_uid is None:
        expected_uid = os.geteuid()
    parent = os.path.dirname(path)
    base = os.path.dirname(parent)
    if not os.path.isdir(base):
        os.mkdir(base, 0o700)
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(parent)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise PermissionError("socket rendezvous boundary mismatch")
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(path)
        listener.listen()
        return listener
    except BaseException:
        listener.close()
        raise


def register_startup_stage(name, callback):
    if name not in {"run-identity", "top-seat", "recovery"}:
        raise ValueError("unknown startup stage")
    if name in _STARTUP_STAGES:
        raise ValueError("startup stage already registered")
    _STARTUP_STAGES[name] = callback


def _trace(trace, value):
    if trace is not None:
        trace.append(value)


def _write_state(root, state):
    data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode(
        "utf-8") + b"\n"
    with TempWrite(root, ".engine/daemon.json") as pending:
        pending.write(data)
        pending.rename_replace()


def _pid_start_time(pid):
    if pid == os.getpid():
        return _PROCESS_STARTED
    if os.path.exists("/proc/%d/stat" % pid):
        try:
            with open("/proc/%d/stat" % pid, encoding="ascii") as source:
                value = source.read()
        except OSError:
            return None
        closing = value.rfind(")")
        fields = value[closing + 2:].split()
        return fields[19] if len(fields) > 19 else None
    return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _allow_stale_cleanup(root):
    try:
        record = json.loads(root.open_read(".engine/daemon.json"))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return True
    pid = record.get("pid")
    recorded_start = record.get("pid_start_time")
    if not isinstance(pid, int):
        return False
    if not _pid_alive(pid):
        return True
    actual_start = _pid_start_time(pid)
    return actual_start is not None and actual_start != recorded_start


def _prepare_runtime_socket(path, cleanup_allowed):
    if os.path.lexists(path):
        if not cleanup_allowed:
            raise BlockingIOError(errno.EWOULDBLOCK,
                                  "live daemon socket retained")
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode):
            raise OSError(errno.EPERM, "non-socket rendezvous retained")
        os.unlink(path)
    return prepare_socket(path)


class _SocketService:
    def __init__(self, listener, writer):
        self.listener = listener
        self.writer = writer
        self.stopping = threading.Event()
        self.connections = []
        self._lock = threading.Lock()

    def _send(self, connection, value):
        connection.sendall(encode_frame(value))

    def _connection(self, connection):
        decoder = FrameDecoder()
        connection.settimeout(2.0)
        try:
            while True:
                try:
                    data = connection.recv(65536)
                except socket.timeout:
                    return
                if not data:
                    return
                try:
                    frames = decoder.feed(data)
                except WireFault as fault:
                    self._send(connection, error_response(None, fault.error))
                    return
                for frame in frames:
                    try:
                        request = decode_request(frame)
                    except WireFault as fault:
                        request_id = None
                        try:
                            parsed = json.loads(frame.decode("utf-8"))
                            if isinstance(parsed, dict):
                                request_id = parsed.get("id")
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                        self._send(connection, error_response(
                            request_id, fault.error))
                        if fault.error.code == "E-FRAMING":
                            return
                        continue
                    try:
                        future = self.writer.enqueue(request)
                    except errors.EngineError as exc:
                        self._send(connection, error_response(
                            request["id"], exc))
                        continue
                    if request["op"] == "daemon.stop":
                        self.writer.begin_stop()
                        self.stopping.set()
                        self.listener.close()
                    try:
                        result = future.result()
                    except errors.EngineError as exc:
                        response = error_response(request["id"], exc)
                    else:
                        response = success_response(request["id"], result)
                    self._send(connection, response)
        finally:
            connection.close()

    def serve(self):
        self.listener.settimeout(0.2)
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    break
                raise
            thread = threading.Thread(target=self._connection,
                                      args=(connection,), daemon=True)
            with self._lock:
                self.connections.append(thread)
            thread.start()
        for thread in self.connections:
            thread.join(2.5)


def _submit_handler(ledger, root, args):
    try:
        body = base64.b64decode(args["body_b64"], validate=True)
        envelope = parse_draft(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, errors.EngineError) as exc:
        raise errors.error_for("E-ENVELOPE") from exc
    if args["envelope"] != envelope.headers:
        raise errors.error_for("E-ENVELOPE")
    if not seats.tag_is_current(
            ledger, root, envelope.from_seat, args["tag"]):
        raise errors.error_for("E-KEY-MISMATCH")
    cycle_events, cycle_advisories = cycles.prepare(
        ledger, envelope, args.get("admits_against"))
    supersession_edges, supersession_advisories = supersede.prepare(
        ledger, envelope)
    admission = admit(
        ledger, root, envelope, body, args["submission_id"],
        claimed_body_sha256=args["body_sha256"],
        claimed_content_hash=args["content_hash"],
        admits_against=args.get("admits_against"),
        prechecks=(cycles.callback(),),
        cycle_events=cycle_events,
        supersession_edges=supersession_edges,
        advisories=tuple(cycle_advisories) + tuple(
            supersession_advisories) + rules.advisories_for(envelope))
    rendered = render_relay(ledger, root, admission.seq)
    render_index(root, index_rows(ledger), 10, epoch_state(ledger))
    advisory_row = ledger.execute(
        "SELECT advisories_json FROM relays WHERE seq=?",
        (admission.seq,)).fetchone()
    return {"path": admission.rendered_path,
            "render_state": ("rendered" if rendered.event == "rendered"
                             else "render-conflict"),
            "advisories": json.loads(advisory_row[0]),
            "duplicate": admission.replay}


def _default_handler(ledger, root, state, handlers, request):
    op = request["op"]
    if op in handlers:
        return handlers[op](request["args"])
    if op == "submit":
        return _submit_handler(ledger, root, request["args"])
    if op in {"seat.register", "seat.replace"}:
        args = request["args"]
        replacing = op == "seat.replace" or args.get("replace", False)
        return seats.register(
            ledger, root, args["address"], args["role_word"],
            args.get("dispatch_ref"), replace=replacing)
    if op == "seat.stand_down":
        return seats.stand_down(ledger, root, request["args"]["address"])
    if op == "seat.show":
        return seats.show(ledger, request["args"]["address"])
    if op == "roster":
        return seats.roster(ledger, root)
    if op == "commission":
        args = request["args"]
        return commission.commission(
            ledger, root, args["dispatch_relay_path"], args["child_run_id"])
    if op == "adopt_commission":
        try:
            record = base64.b64decode(
                request["args"]["record_b64"], validate=True)
        except ValueError as exc:
            raise errors.error_for("E-ENVELOPE") from exc
        return commission.adopt(ledger, record)
    if op == "export_ruling":
        args = request["args"]
        return supersede.export_ruling(
            ledger, args["ruling_path"], args["for_child"])
    if op == "adopt_ruling":
        try:
            bundle = base64.b64decode(
                request["args"]["bundle_b64"], validate=True)
        except ValueError as exc:
            raise errors.error_for("E-ENVELOPE") from exc
        return supersede.adopt_ruling(ledger, root, bundle)
    if op == "show":
        value = reconcile.show(ledger, request["args"]["target"])
        body = value.pop("body")
        if request["args"].get("body", False):
            value["body_b64"] = base64.b64encode(body).decode("ascii")
        return value
    if op == "render":
        return reconcile.render_all(ledger, root)
    if op == "verify":
        return reconcile.verify(ledger, root)
    if op == "reconcile":
        return reconcile.reconcile(ledger, root)
    if op == "daemon.stop":
        return {"ok": True}
    if op == "status":
        return reconcile.status(ledger, state)
    raise _args_fault(op, "op:unavailable").error


def _run_daemon(root_path, ready_fd, handlers, trace, socket_override,
                top_seat, top_role, top_dispatch, run_id,
                commissioning_record):
    global _log_fd
    root = None
    lease = None
    log_fd = None
    ledger = None
    listener = None
    state = None
    socket_name = None
    socket_owned = False
    ready_stream = None
    writer = None
    fresh_ledger = False
    try:
        root = Root(root_path)
        _trace(trace, "open-root")
        lease = acquire_lease(root)
        _trace(trace, "ensure-engine")
        _trace(trace, "flock")
        socket_name = socket_path(root.path, socket_override)
        cleanup_allowed = (not os.path.lexists(socket_name) or
                           _allow_stale_cleanup(root))
        state = {
            "pid": os.getpid(), "pid_start_time": _pid_start_time(os.getpid()),
            "nonce": str(uuid.uuid4()), "state": "starting",
            "socket": socket_name, "version": 1, "schema_version": 1,
        }
        _write_state(root, state)
        _trace(trace, "starting-record")
        log_fd = open_log(lease.engine_dirfd)
        _log_fd = log_fd
        strings.set_diagnostic_sink(_log_diagnostic)
        _trace(trace, "log-bound")
        holder = {}
        writer = SerialWriter(lambda request: _default_handler(
            holder["ledger"], root, state, handlers, request))
        writer.start()
        _trace(trace, "writer-started")
        try:
            os.stat("ledger.db", dir_fd=lease.engine_dirfd,
                    follow_symlinks=False)
        except FileNotFoundError:
            ledger = init_schema(lease.engine_dirfd, root.path)
            fresh_ledger = True
        else:
            ledger = open_ledger(lease.engine_dirfd)
        holder["ledger"] = ledger
        _trace(trace, "schema-init")
        context = {"root": root, "lease": lease, "ledger": ledger,
                   "state": state, "top_seat": top_seat,
                   "top_role": top_role, "top_dispatch": top_dispatch,
                   "run_id": run_id,
                   "commissioning_record": commissioning_record}
        for stage in ("run-identity", "top-seat", "recovery"):
            callback = _STARTUP_STAGES.get(stage)
            if callback is None:
                _trace(trace, stage + ":absent")
            else:
                try:
                    callback(context)
                except BaseException:
                    if stage == "run-identity" and fresh_ledger:
                        writer.begin_stop()
                        writer.join(5)
                        ledger.close()
                        ledger = None
                        for name in ("ledger.db-wal", "ledger.db-shm",
                                     "ledger.db"):
                            try:
                                os.unlink(name, dir_fd=lease.engine_dirfd)
                            except FileNotFoundError:
                                pass
                        os.fsync(lease.engine_dirfd)
                    raise
                _trace(trace, stage + ":present")
        listener = _prepare_runtime_socket(socket_name, cleanup_allowed)
        socket_owned = True
        _trace(trace, "socket-bound")
        state["state"] = "ready"
        _write_state(root, state)
        _trace(trace, "ready-record")
        if ready_fd is not None:
            ready_stream = io.FileIO(ready_fd, mode="wb", closefd=True)
            ready_stream.write(b"R")
            ready_stream.flush()
            _trace(trace, "ready-byte")
            ready_stream.close()
            ready_stream = None
            ready_fd = None
            _trace(trace, "ready-pipe-closed")
        service = _SocketService(listener, writer)
        service.serve()
        writer.begin_stop()
        writer.join(5)
        ledger.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        state["state"] = "stopped"
        _write_state(root, state)
        return 0
    finally:
        if writer is not None:
            writer.begin_stop()
            writer.join(5)
        if ready_stream is not None:
            ready_stream.close()
        elif ready_fd is not None:
            os.close(ready_fd)
        if listener is not None:
            listener.close()
        if socket_owned and socket_name is not None and os.path.lexists(socket_name):
            try:
                os.unlink(socket_name)
            except OSError:
                pass
        if socket_owned and socket_name is not None:
            rendezvous = os.path.dirname(socket_name)
            try:
                os.rmdir(rendezvous)
            except OSError:
                pass
        if ledger is not None:
            ledger.close()
        strings.set_diagnostic_sink(None)
        _log_fd = None
        if log_fd is not None:
            os.close(log_fd)
        if lease is not None:
            lease.close()
        if root is not None:
            root.close()


def _close_inherited(keep):
    keep = set(keep)
    directory = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
    for name in os.listdir(directory):
        try:
            fd = int(name)
        except ValueError:
            continue
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def launch(root_path, handlers=None, socket_override=None, timeout=10.0,
           top_seat=None, top_role=None, top_dispatch=None, run_id=None,
           commissioning_record=None):
    read_fd, write_fd = os.pipe()
    first = os.fork()
    if first == 0:
        try:
            os.close(read_fd)
            os.setsid()
            second = os.fork()
            if second > 0:
                os.close(write_fd)
                os._exit(0)
            _close_inherited({write_fd})
            try:
                start(root_path, ready_fd=write_fd, handlers=handlers,
                      socket_override=socket_override, top_seat=top_seat,
                      top_role=top_role, top_dispatch=top_dispatch,
                      run_id=run_id,
                      commissioning_record=commissioning_record)
            except BaseException:
                os._exit(1)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write_fd)
    os.waitpid(first, 0)
    ready, _, _ = select.select([read_fd], [], [], timeout)
    if not ready:
        os.close(read_fd)
        return False
    data = os.read(read_fd, 1)
    os.close(read_fd)
    return data == b"R"
