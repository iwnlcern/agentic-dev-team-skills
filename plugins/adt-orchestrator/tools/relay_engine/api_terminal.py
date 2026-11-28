"""Unsigned wire carriers for the terminal operator confirmation service."""
from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import json
import secrets
import threading
import time
from typing import TYPE_CHECKING

from relay_engine import api_compose, api_definitions as d, jcs, operator_terminal
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.api_protocol import ComponentRefusal

if TYPE_CHECKING:
    from relay_engine.api_submit import ServiceContext


_CHALLENGE = 'operator.terminal-challenge'
_ACT = 'operator.terminal-act'
_LIFETIME_NS = 120_000_000_000
_MAX_OUTSTANDING = 16
_TYPES = (('TerminalArgsBase64', 10240), ('TerminalSignatureBase64', 1024),
          ('TerminalResultBase64', 65536))


class TerminalActs:
    def __init__(self):
        self._handlers = {}

    def register(self, purpose: str, handler: Callable[[OperatorPrincipal, Mapping],
                 Mapping | ComponentRefusal], *, max_result_bytes: int) -> None:
        if (purpose not in operator_terminal.PURPOSES or purpose in self._handlers
                or type(max_result_bytes) is not int or not 1 <= max_result_bytes <= 65536):
            raise ValueError('invalid terminal registration')
        self._handlers[purpose] = (handler, max_result_bytes)

    def handler(self, purpose: str) -> tuple[Callable, int] | None:
        return self._handlers.get(purpose)


@dataclass(slots=True)
class _Challenge:
    value: bytes
    store_id: str
    issued_ns: int
    spent: bool = False


def _store_id(conn):
    with closing(conn.execute('SELECT store_id FROM catalogue_identity WHERE id=1')) as cursor:
        return cursor.fetchone()[0]


class TerminalService:
    def __init__(self, context: ServiceContext, acts: TerminalActs, *,
                 clock: Callable[[], int] = time.monotonic_ns,
                 verify: Callable[[bytes, bytes], OperatorPrincipal] = operator_terminal._principal_after_verify):
        self._context = context
        self._acts = acts
        self._clock = clock
        self._verify = verify
        self._lock = threading.Lock()
        self._challenges: dict[str, _Challenge] = {}

    def challenge(self) -> Mapping | ComponentRefusal:
        with self._lock:
            now = self._clock()
            if sum(not row.spent and now - row.issued_ns < _LIFETIME_NS
                   for row in self._challenges.values()) >= _MAX_OUTSTANDING:
                return ComponentRefusal('E_TERMINAL_LIMIT', 'challenge_limit')
            store_id = self._context.port.read_catalogue(_store_id)
            challenge_id = secrets.token_hex(16)
            while challenge_id in self._challenges:
                challenge_id = secrets.token_hex(16)
            value = secrets.token_bytes(32)
            self._challenges[challenge_id] = _Challenge(value, store_id, self._clock())
        return {'challenge_id': challenge_id, 'value': value.hex(), 'store_id': store_id}

    def act(self, args: Mapping) -> Mapping | ComponentRefusal:
        try:
            raw = base64.b64decode(args['args_base64'], validate=True)
            parsed = json.loads(raw)
            canonical = isinstance(parsed, dict) and jcs.jcs_encode(parsed) == raw
        except (ValueError, TypeError, RecursionError):
            canonical = False
        if not canonical:
            return ComponentRefusal('E_TERMINAL_INVALID', 'args_not_canonical')

        purpose = args['purpose']
        with self._lock:
            record = self._challenges.get(args['challenge_id'])
            if record is None:
                return ComponentRefusal('E_TERMINAL_CHALLENGE', 'challenge_unknown')
            if record.spent:
                return ComponentRefusal('E_TERMINAL_CHALLENGE', 'challenge_used')
            if self._clock() - record.issued_ns >= _LIFETIME_NS:
                return ComponentRefusal('E_TERMINAL_CHALLENGE', 'challenge_expired')
            record.spent = True
            message = operator_terminal.build_message(purpose, parsed,
                                                       store_id=record.store_id, value=record.value)
        try:
            principal = self._verify(message, base64.b64decode(args['signature_base64'], validate=True))
        except operator_terminal.OperatorCheckRefused as error:
            return ComponentRefusal('E_TERMINAL_CHECK', error.reason)
        handler = self._acts.handler(purpose)
        if handler is None:
            return ComponentRefusal('E_TERMINAL_INVALID', 'purpose_unhandled')
        run, limit = handler
        result = run(principal, parsed)
        if isinstance(result, ComponentRefusal):
            return result
        if not isinstance(result, Mapping):
            return ComponentRefusal('E_API_INTERNAL', 'handler_fault')
        encoded = jcs.jcs_encode(dict(result))
        if len(encoded) > limit:
            return ComponentRefusal('E_API_INTERNAL', 'handler_fault')
        return {'purpose': purpose, 'result_base64': base64.b64encode(encoded).decode('ascii')}


def terminal_definitions() -> d.DefinitionGraph:
    ref = d.TypeRef

    def record(name, fields):
        return d.TypeDef(name, d.Record(tuple(d.FieldDef(key, ref(type_name))
                                              for key, type_name in fields)))

    types = (
        record('TerminalChallengeArgs', ()),
        d.TypeDef('TerminalChallengeId', d.String('[0-9a-f]{32}', 32, 32)),
        d.TypeDef('TerminalValue', d.String('[0-9a-f]{64}', 64, 64)),
        record('TerminalChallengeResult', (('challenge_id', 'TerminalChallengeId'),
              ('value', 'TerminalValue'), ('store_id', 'OpaqueId'))),
        d.TypeDef('TerminalPurpose', d.LiteralSet(operator_terminal.PURPOSES)),
        record('TerminalActArgs', (('challenge_id', 'TerminalChallengeId'), ('purpose', 'TerminalPurpose'),
              ('args_base64', 'TerminalArgsBase64'), ('signature_base64', 'TerminalSignatureBase64'))),
        record('TerminalActResult', (('purpose', 'TerminalPurpose'), ('result_base64', 'TerminalResultBase64'))),
    ) + tuple(d.TypeDef(name, d.String(r'[A-Za-z0-9+/]*={0,2}', 0, 4 * ((limit + 2) // 3)),
                        (d.CanonicalBase64(ref(name), limit),)) for name, limit in _TYPES)
    error = ref('ApiError')
    return d.DefinitionGraph(types, (
        d.OperationDef(_CHALLENGE, ref('TerminalChallengeArgs'), ref('TerminalChallengeResult'),
                       error, 'unsigned', 'operator_challenge', 'none', 'none'),
        d.OperationDef(_ACT, ref('TerminalActArgs'), ref('TerminalActResult'),
                       error, 'unsigned', 'operator_confirmed_act', 'none', 'none'),
    ), error)


def _challenge(_args, _principal, port):
    return port.challenge()


def _act(args, _principal, port):
    return port.act(args)


def terminal_handlers() -> Mapping[str, Callable]:
    return {_CHALLENGE: _challenge, _ACT: _act}


def terminal_component(acts: TerminalActs) -> api_compose.ApiComponent:
    return api_compose.ApiComponent('terminal', terminal_definitions(), terminal_handlers(),
                                    lambda context: TerminalService(context, acts))
