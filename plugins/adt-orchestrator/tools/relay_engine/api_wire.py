"""Strict discovery JSON and framing; parsing never invokes an operation handler."""
from collections.abc import Mapping
from dataclasses import dataclass
import json

from relay_engine import api_definitions as d, api_registry as r, jcs

MAX_API_FRAME = 16777216
JSON_ERROR_KINDS = frozenset({'bom', 'invalid-utf8', 'duplicate-key', 'lone-surrogate',
                              'number', 'trailing-data', 'syntax'})


class StrictJsonError(ValueError):
    def __init__(self, kind: str):
        if kind not in JSON_ERROR_KINDS:
            raise ValueError('invalid JSON error kind')
        self.kind = kind
        super().__init__(kind)


class ApiFrameError(ValueError):
    """An unusable frame decoder; messages contain only fixed refusal labels."""


@dataclass(frozen=True, slots=True)
class ApiRequest:
    request_id: str
    op: str
    args: object


@dataclass(frozen=True, slots=True)
class ProtocolRefusal:
    request_id: str | None
    cause_kind: str
    close: bool


def strict_json_loads(data: bytes) -> object:
    if type(data) is not bytes:
        raise StrictJsonError('syntax')
    if data.startswith(b'\xef\xbb\xbf'):
        raise StrictJsonError('bom')
    try:
        text = data.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        raise StrictJsonError('invalid-utf8') from None
    def number(_value):
        raise StrictJsonError('number')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise StrictJsonError('duplicate-key')
            value[key] = item
        return value
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_int=number,
                           parse_float=number, parse_constant=number)
    except json.JSONDecodeError as error:
        raise StrictJsonError('trailing-data' if error.msg == 'Extra data' else 'syntax') from None
    except RecursionError:
        raise StrictJsonError('syntax') from None
    def scalars(item, depth=0):
        if depth > d.MAX_JSON_DEPTH:
            raise StrictJsonError('syntax')
        if type(item) is str:
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise StrictJsonError('lone-surrogate')
        elif type(item) is dict:
            for key, child in item.items():
                scalars(key, depth + 1)
                scalars(child, depth + 1)
        elif type(item) is list:
            for child in item:
                scalars(child, depth + 1)
    scalars(value)
    return value


class ApiFrameDecoder:
    def __init__(self):
        self._buffer = bytearray()
        self._wanted = None
        self._closed = False

    def feed(self, data: bytes) -> list[bytes]:
        if self._closed:
            raise ApiFrameError('closed_decoder')
        if type(data) is not bytes:
            self._closed = True
            raise ApiFrameError('invalid_chunk')
        self._buffer.extend(data)
        frames, offset = [], 0
        while True:
            if self._wanted is None:
                if len(self._buffer) - offset < 4:
                    break
                self._wanted = int.from_bytes(self._buffer[offset:offset + 4], 'big')
                offset += 4
                if not 1 <= self._wanted <= MAX_API_FRAME:
                    self._closed = True
                    self._buffer.clear()
                    raise ApiFrameError('invalid_length')
            if len(self._buffer) - offset < self._wanted:
                break
            frames.append(bytes(self._buffer[offset:offset + self._wanted]))
            offset += self._wanted
            self._wanted = None
        del self._buffer[:offset]
        return frames

    def finish(self) -> None:
        if self._closed:
            raise ApiFrameError('closed_decoder')
        self._closed = True
        if self._buffer or self._wanted is not None:
            self._buffer.clear()
            raise ApiFrameError('partial_frame')


def decode_api_frame(body: bytes, registry) -> ApiRequest | ProtocolRefusal:
    if type(body) is not bytes or not 1 <= len(body) <= MAX_API_FRAME:
        return ProtocolRefusal(None, 'malformed_frame', True)
    try:
        envelope = strict_json_loads(body)
    except StrictJsonError:
        return ProtocolRefusal(None, 'malformed_json', True)
    if type(envelope) is not dict or set(envelope) != {'request_id', 'op', 'args'}:
        return ProtocolRefusal(None, 'invalid_envelope', True)
    for field, ref in (('request_id', 'RequestId'), ('op', 'OperationName')):
        if isinstance(r.validate_type(registry, d.TypeRef(ref), envelope[field]), r.ValidationFailure):
            return ProtocolRefusal(None, 'invalid_envelope', True)
    # Argument checks follow id/phase/lookup in dispatch, preserving their precedence.
    return ApiRequest(envelope['request_id'], envelope['op'], envelope['args'])


def encode_api_response(response: Mapping, registry, operation: str | None,
                        context: r.DiscoveryValidationContext | None) -> bytes:
    if not isinstance(response, Mapping):
        raise ValueError('invalid_response')
    envelope = dict(response)
    keys = set(envelope)
    if keys not in ({'request_id', 'result'}, {'request_id', 'error'}):
        raise ValueError('invalid_response')
    request_id = envelope['request_id']
    if request_id is not None:
        if isinstance(r.validate_type(registry, d.TypeRef('RequestId'), request_id), r.ValidationFailure):
            raise ValueError('invalid_response')
    if 'result' in envelope:
        if request_id is None or operation is None:
            raise ValueError('invalid_response')
        checked = r.validate_operation_result(registry, operation, envelope['result'], context)
    else:
        op = registry.operation(operation) if operation is not None else None
        checked = r.validate_type(registry, op.error_ref if op is not None else d.TypeRef('ApiError'), envelope['error'])
    if isinstance(checked, r.ValidationFailure):
        raise ValueError('invalid_response')
    try:
        raw = jcs.jcs_encode(envelope)
    except (ValueError, TypeError):
        raise ValueError('invalid_response') from None
    if not 1 <= len(raw) <= MAX_API_FRAME:
        raise ApiFrameError('invalid_length')
    return len(raw).to_bytes(4, 'big') + raw
