"""Explicit local API transport with endpoint identity and private-key guards."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time

from relay_engine import api_definitions as d, api_platform, api_registry as r
from relay_engine import api_schema, api_wire, jcs

LOCAL_REFUSALS = frozenset({'endpoint_not_selected', 'ready_file_invalid', 'ready_file_mismatch',
    'key_file_invalid', 'key_file_unprotected', 'mixed_credentials', 'no_session',
    'draft_unreadable', 'draft_too_large'})
ENDPOINT_OUTCOMES = frozenset({'not_performed', 'unknown', 'endpoint_mismatch'})


class LocalRefusal(Exception):
    def __init__(self, kind: str):
        if kind not in LOCAL_REFUSALS:
            raise ValueError('invalid_local_refusal')
        self.kind = kind
        super().__init__(kind)


class EndpointUnavailable(Exception):
    def __init__(self, outcome: str):
        if outcome not in ENDPOINT_OUTCOMES:
            raise ValueError('invalid_endpoint_outcome')
        self.outcome = outcome
        super().__init__(outcome)


@dataclass(frozen=True, slots=True)
class ReadyFile:
    path: str
    socket: str
    store_id: str
    build_id: str
    schema_digest: str
    pid: int


@dataclass(frozen=True, slots=True)
class SeatKey:
    key_id: str
    key_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApiErrorValue:
    code: str
    cls: str
    cause: Mapping
    remedy: str


@cache
def _registry_and_digest():
    registry = api_platform.build_platform().registry
    return registry, api_schema.export_schema(registry, '3.0').digest


def _valid(name, value):
    registry, _ = _registry_and_digest()
    return not isinstance(r.validate_type(registry, d.TypeRef(name), value), r.ValidationFailure)


def read_ready_file(path: str) -> ReadyFile:
    if not path:
        raise LocalRefusal('endpoint_not_selected')
    try:
        target = Path(path).absolute()
        parent = target.parent.stat()
        status = os.stat(target, follow_symlinks=False)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
                or not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_mode & 0o022):
            raise LocalRefusal('ready_file_invalid')
        with open(target, 'rb', opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW)) as stream:
            actual = os.fstat(stream.fileno())
            if (actual.st_dev, actual.st_ino) != (status.st_dev, status.st_ino):
                raise LocalRefusal('ready_file_invalid')
            data = json.loads(stream.read(16385))
        if (type(data) is not dict
                or not {'format', 'socket', 'store_id', 'build_id', 'schema_digest', 'pid'} <= set(data)
                or data['format'] != 'adt-ready-v1' or type(data['socket']) is not str
                or not Path(data['socket']).is_absolute()
                or Path(data['socket']).resolve().parent != target.parent.resolve()
                or not _valid('OpaqueId', data['store_id'])
                or not _valid('Digest', data['build_id'])
                or not _valid('Digest', data['schema_digest'])
                or type(data['pid']) is not int or data['pid'] < 1):
            raise LocalRefusal('ready_file_invalid')
        return ReadyFile(str(target), data['socket'], data['store_id'], data['build_id'],
                         data['schema_digest'], data['pid'])
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalRefusal('ready_file_invalid') from None


def read_key_file(path: str) -> SeatKey:
    try:
        status = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            raise LocalRefusal('key_file_invalid')
        if status.st_uid != os.getuid() or status.st_mode & 0o077:
            raise LocalRefusal('key_file_unprotected')
        with open(path, 'rb', opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW)) as stream:
            actual = os.fstat(stream.fileno())
            if ((actual.st_dev, actual.st_ino) != (status.st_dev, status.st_ino)
                    or actual.st_uid != os.getuid() or actual.st_mode & 0o077):
                raise LocalRefusal('key_file_unprotected')
            raw = stream.read(4097)
        parts = raw.decode('ascii').removesuffix('\n').split(' ')
        if (len(parts) != 3 or parts[0] != 'adt-seat-key-v1'
                or not raw.endswith(b'\n') or raw.count(b'\n') != 1
                or not _valid('KeyId', parts[1]) or not _valid('KeySecret', parts[2])):
            raise LocalRefusal('key_file_invalid')
        return SeatKey(parts[1], parts[2])
    except (OSError, ValueError, TypeError):
        raise LocalRefusal('key_file_invalid') from None


class ApiClient:
    def __init__(self, connection, registry, digest):
        self._socket = connection
        self._registry = registry
        self._digest = digest
        self._request_id = 0
        self._lock = threading.Lock()
        self._decoder = api_wire.ApiFrameDecoder()

    @classmethod
    def open(cls, ready: ReadyFile) -> 'ApiClient':
        registry, digest = _registry_and_digest()
        if ready.schema_digest != digest:
            raise LocalRefusal('ready_file_mismatch')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(30)
        try:
            connection.connect(ready.socket)
        except OSError:
            connection.close()
            raise EndpointUnavailable('not_performed') from None
        client = cls(connection, registry, digest)
        try:
            client._discover(ready)
            return client
        except BaseException:
            client.close()
            raise

    def _discover(self, ready, *, instance=None, deadline=None):
        if ready.schema_digest != self._digest:
            raise LocalRefusal('ready_file_mismatch')
        arguments = {'supported': {'min': '3.0', 'max': '3.0'},
                     'required_schema_digest': self._digest, 'required_build_id': ready.build_id}
        result = (self.call('protocol.connect', arguments) if deadline is None else
                  self._call('protocol.connect', arguments, deadline=deadline))
        expected = {'store_id': ready.store_id, 'build_id': ready.build_id, 'schema_digest': ready.schema_digest}
        if instance is not None:
            expected['runtime_instance_id'] = instance
        if isinstance(result, ApiErrorValue) or any(result[name] != value for name, value in expected.items()):
            raise EndpointUnavailable('endpoint_mismatch')

    def call(self, operation: str, args: Mapping) -> Mapping | ApiErrorValue:
        return self._call(operation, args)

    def _call(self, operation, args, *, deadline=None):
        def bound_timeout():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                self._socket.settimeout(remaining)

        with self._lock:
            self._request_id += 1
            request_id = str(self._request_id)
            try:
                raw = jcs.jcs_encode({'request_id': request_id, 'op': operation, 'args': dict(args)})
                bound_timeout()
                self._socket.sendall(len(raw).to_bytes(4, 'big') + raw)
                frames = []
                while not frames:
                    bound_timeout()
                    chunk = self._socket.recv(65536)
                    if not chunk:
                        raise EndpointUnavailable('unknown')
                    frames = self._decoder.feed(chunk)
                if len(frames) != 1:
                    raise EndpointUnavailable('unknown')
                response = api_wire.strict_json_loads(frames[0])
                if (type(response) is not dict
                        or set(response) not in ({'request_id', 'result'}, {'request_id', 'error'})
                        or response['request_id'] != request_id):
                    raise EndpointUnavailable('unknown')
                if 'error' in response:
                    error = response['error']
                    if not _valid('ApiError', error):
                        raise EndpointUnavailable('unknown')
                    return ApiErrorValue(error['code'], error['class'], error['cause'], error['remedy'])
                context = r.DiscoveryValidationContext('schema_reply', '3.0', self._digest)
                if isinstance(r.validate_operation_result(self._registry, operation,
                        response['result'], context), r.ValidationFailure):
                    raise EndpointUnavailable('unknown')
                return response['result']
            except (OSError, ValueError, TypeError, KeyError, EndpointUnavailable):
                self.close()
                raise EndpointUnavailable('unknown') from None

    def identify(self, key: SeatKey) -> Mapping | ApiErrorValue:
        return self.call('seat.identify', {'key_id': key.key_id, 'key_secret': key.key_secret})

    def sign_in(self, key: SeatKey, host: str, host_session_id: str,
                operation_key: str) -> Mapping | ApiErrorValue:
        return self.call('session.sign-in', {'key_id': key.key_id, 'key_secret': key.key_secret,
            'host': host, 'host_session_id': host_session_id, 'operation_key': operation_key})

    def close(self) -> None:
        self._socket.close()
