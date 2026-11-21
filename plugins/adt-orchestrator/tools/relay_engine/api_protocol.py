"""Pure discovery handlers and immutable per-connection protocol transitions."""
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import cache
import re
from types import MappingProxyType
from typing import Protocol

from relay_engine import api_definitions as d, api_registry as r, api_schema, api_wire as w, errors, strings
from relay_engine.api_principals import OperatorPrincipal, ServicePrincipal

IMPLEMENTED_PROTOCOLS = ('3.0',)
DECODER_REFUSAL_KINDS = frozenset({'malformed_frame', 'malformed_json', 'invalid_envelope'})


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    store_id: str
    store_kind: str
    runtime_instance_id: str
    engine_version: str
    build_id: str
    ready: bool
    schemas: Mapping[str, api_schema.SchemaArtifact]

    def __post_init__(self):
        schemas = dict(self.schemas)
        def digest_shape(value):
            return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None
        if (not digest_shape(self.build_id) or any(
                protocol != artifact.protocol or not digest_shape(artifact.digest)
                for protocol, artifact in schemas.items())):
            raise ValueError('invalid_runtime_context')
        object.__setattr__(self, 'schemas', MappingProxyType(schemas))


@dataclass(frozen=True, slots=True)
class SeatBinding:
    project_id: str
    team_instance_id: str
    seat_id: str
    occupant_id: str
    key_id: str
    session_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class SessionRefusal:
    code: str
    cause_kind: str


@dataclass(frozen=True, slots=True)
class ComponentRefusal:
    code: str
    cause_kind: str
    payload: Mapping | None = None


PLATFORM_SESSION_OPERATIONS = frozenset({'session.sign-in', 'session.resume', 'session.end'})
CLEARING_AUTH_CAUSES = frozenset({'key_replaced', 'seat_stood_down', 'session_not_current'})


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    result: Mapping
    binding_action: str
    binding: SeatBinding | None


class SessionPort(Protocol):
    def run(self, operation: str, args: Mapping,
            binding: SeatBinding | None) -> SessionOutcome | SessionRefusal | Mapping: ...


class SubmitPort(Protocol):
    def run(self, args: Mapping, binding: SeatBinding) -> Mapping | ComponentRefusal: ...


SessionHandler = Callable[[Mapping, SeatBinding | None, SessionPort], SessionOutcome | SessionRefusal | Mapping]


@dataclass(frozen=True, slots=True)
class ConnectionState:
    phase: str
    last_request_id: int | None
    negotiated_protocol: str | None
    negotiated_schema_digest: str | None
    binding: SeatBinding | None = None


@dataclass(frozen=True, slots=True)
class ResponseAndConnectionState:
    response: Mapping | None
    state: ConnectionState
    close: bool


def initial_connection_state() -> ConnectionState:
    return ConnectionState('awaiting_connect', None, None, None)


def api_error(registry, cause_kind: str, **payload) -> dict:
    """Resolve the complete cause/remedy arm from Engine's compiled catalogue."""
    for arm in registry.error_arms():
        cause = registry.types[arm.cause_type.name].shape
        for field in cause.fields:
            if field.name == 'kind':
                kinds = registry.types[field.type_ref.name].shape.values
                if cause_kind in kinds:
                    value = {'code': arm.code, 'class': arm.wire_class,
                             'cause': {'kind': cause_kind, **payload}, 'remedy': arm.remedy}
                    if isinstance(r.validate_type(registry, d.TypeRef('ApiError'), value), r.ValidationFailure):
                        raise ValueError('invalid_api_error')
                    return value
    raise ValueError('unknown_api_error')


def _connect(_args, runtime, state, registry):
    return {'protocol': state.negotiated_protocol, 'engine_version': runtime.engine_version,
            'build_id': runtime.build_id, 'schema_digest': state.negotiated_schema_digest,
            'store_id': runtime.store_id, 'store_kind': runtime.store_kind,
            'runtime_instance_id': runtime.runtime_instance_id,
            'supported': {'min': IMPLEMENTED_PROTOCOLS[0], 'max': IMPLEMENTED_PROTOCOLS[-1]},
            'capabilities': [{'operation': name} for name in registry.capabilities()],
            'deprecation': None}


def _schema(_args, runtime, state, _registry):
    artifact = runtime.schemas[state.negotiated_protocol]
    return {'protocol': artifact.protocol, 'schema_digest': artifact.digest,
            'schema_encoding': 'base64', 'schema_byte_count': str(artifact.byte_count),
            'schema_bytes_base64': base64.b64encode(artifact.canonical_bytes).decode('ascii')}


def discovery_handlers() -> dict:
    """The actual handlers shared by dispatch and schema generation."""
    return {'protocol.connect': _connect, 'protocol.schema': _schema}


@cache
def discovery_registry() -> r.FrozenApiRegistry:
    """Compile the immutable core graph once for all discovery consumers."""
    return r.compile_registry(d.discovery_definitions(), discovery_handlers(), (errors.api_error_catalog(),))


def negotiate_protocol(window, implemented=None) -> str | None:
    """Return the highest numeric common protocol, independent of input order."""
    candidates = IMPLEMENTED_PROTOCOLS if implemented is None else implemented
    return max((version for version in candidates
                if _version(window['min']) <= _version(version) <= _version(window['max'])),
               key=_version, default=None)


def _version(value):
    return tuple(int(part) for part in value.split('.'))


def dispatch_discovery(state: ConnectionState, request: w.ApiRequest | w.ProtocolRefusal,
                       runtime: RuntimeContext, registry=None) -> ResponseAndConnectionState:
    registry = discovery_registry() if registry is None else registry
    if state.phase == 'closed':
        return ResponseAndConnectionState(None, state, True)

    def refuse(kind, request_id, close, current=state, **payload):
        response = {'request_id': request_id, 'error': api_error(registry, kind, **payload)}
        # Refusals pass exactly the same complete-arm and frame checks as successes.
        w.encode_api_response(response, registry, None, None)
        return ResponseAndConnectionState(response, replace(current, phase='closed') if close else current, close)

    if isinstance(request, w.ProtocolRefusal):
        if request.cause_kind not in DECODER_REFUSAL_KINDS:
            request_id = request.request_id
            if isinstance(r.validate_type(registry, d.TypeRef('RequestId'), request_id), r.ValidationFailure):
                request_id = None
            return refuse('handler_fault', request_id, True)
        return refuse(request.cause_kind, request.request_id, request.close)
    if not isinstance(request, w.ApiRequest):
        return refuse('invalid_envelope', None, True)
    for ref, value in [('RequestId', request.request_id), ('OperationName', request.op)]:
        if isinstance(r.validate_type(registry, d.TypeRef(ref), value), r.ValidationFailure):
            return refuse('invalid_envelope', None, True)
    number = int(request.request_id)
    if state.last_request_id is not None and number <= state.last_request_id:
        return refuse('non_increasing_request_id', request.request_id, True)
    current = replace(state, last_request_id=number)
    if state.phase == 'awaiting_connect' and request.op != 'protocol.connect':
        return refuse('handshake_required', request.request_id, True, current)
    if state.phase == 'connected' and request.op == 'protocol.connect':
        return refuse('already_connected', request.request_id, True, current)
    op = registry.operation(request.op)
    if op is None:
        return refuse('unknown_operation', request.request_id, False, current)
    if isinstance(r.validate_type(registry, op.request_ref, request.args), r.ValidationFailure):
        return refuse('invalid_args', request.request_id, state.phase == 'awaiting_connect', current)

    if request.op == 'protocol.connect':
        window = request.args['supported']
        protocol = negotiate_protocol(window)
        if protocol is None:
            return refuse('protocol_window', request.request_id, True, current,
                          requested=window, supported={'min': IMPLEMENTED_PROTOCOLS[0],
                                                       'max': IMPLEMENTED_PROTOCOLS[-1]})
        artifact = runtime.schemas.get(protocol)
        if artifact is None or artifact.protocol != protocol:
            return refuse('schema_unavailable', request.request_id, True, current)
        for field, actual in [('build_id', runtime.build_id), ('schema_digest', artifact.digest)]:
            required = request.args.get('required_' + field)
            if required is not None and required != actual:
                return refuse('pin_mismatch', request.request_id, True, current,
                              field=field, required=required, actual=actual)
        candidate = replace(current, phase='connected', negotiated_protocol=protocol,
                            negotiated_schema_digest=artifact.digest)
    else:
        candidate = current
    if not runtime.ready:
        return refuse('runtime_not_ready', request.request_id, True, current)
    artifact = runtime.schemas.get(candidate.negotiated_protocol)
    if (artifact is None or artifact.protocol != candidate.negotiated_protocol or
            artifact.digest != candidate.negotiated_schema_digest):
        return refuse('schema_unavailable', request.request_id, True, current)
    context = r.DiscoveryValidationContext('schema_reply', candidate.negotiated_protocol,
                                           candidate.negotiated_schema_digest)
    try:
        result = registry.handlers[request.op](request.args, runtime, candidate, registry)
        response = {'request_id': request.request_id, 'result': result}
        w.encode_api_response(response, registry, request.op, context)
    except Exception:
        # Never pass the original exception, traceback or payload to the diagnostic sink.
        try:
            strings.report_diagnostic(ValueError('E_API_INTERNAL'))
        except Exception:
            pass
        return refuse('handler_fault', request.request_id, state.phase == 'awaiting_connect', current)
    return ResponseAndConnectionState(response, candidate, False)


def _seat_identify(args, principal, port):
    return port.sessions.run('seat.identify', args, None)


def _session_end(args, binding, port):
    return port.sessions.run('session.end', args, binding)


def _session_resume(args, binding, port):
    return port.sessions.run('session.resume', args, binding)


def _session_sign_in(args, binding, port):
    return port.sessions.run('session.sign-in', args, binding)


def session_handlers() -> Mapping[str, SessionHandler]:
    """Stable handler objects shared by dispatch and registry generation."""
    return {'seat.identify': _seat_identify, 'session.end': _session_end, 'session.resume': _session_resume,
            'session.sign-in': _session_sign_in}


def _relay_submit(args, binding, port):
    return port.submit.run(args, binding)


def submit_handlers() -> Mapping[str, Callable]:
    return {'relay.submit': _relay_submit}


def dispatch_api(state: ConnectionState, request: w.ApiRequest | w.ProtocolRefusal,
                 runtime: RuntimeContext, registry: r.FrozenApiRegistry,
                 operation_ports: Mapping[str, object], *,
                 principal: OperatorPrincipal | ServicePrincipal | None = None) -> ResponseAndConnectionState:
    if principal is not None and isinstance(request, w.ApiRequest):
        operation = registry.operation(request.op)
        if operation is not None and operation.principal in {'unsigned', 'seat_key', 'seat_session'}:
            raise ValueError('invalid_in_process_principal')
    if (not isinstance(request, w.ApiRequest)
            or request.op in ('protocol.connect', 'protocol.schema')):
        return dispatch_discovery(state, request, runtime, registry)
    if state.phase == 'closed':
        return ResponseAndConnectionState(None, state, True)

    def refuse(kind, request_id, close=False, current=state):
        response = {'request_id': request_id, 'error': api_error(registry, kind)}
        w.encode_api_response(response, registry, None, None)
        return ResponseAndConnectionState(response,
            replace(current, phase='closed') if close else current, close)

    for ref, value in [('RequestId', request.request_id), ('OperationName', request.op)]:
        if isinstance(r.validate_type(registry, d.TypeRef(ref), value), r.ValidationFailure):
            return refuse('invalid_envelope', None, True)
    number = int(request.request_id)
    if state.last_request_id is not None and number <= state.last_request_id:
        return refuse('non_increasing_request_id', request.request_id, True)
    current = replace(state, last_request_id=number)
    if state.phase != 'connected':
        return refuse('handshake_required', request.request_id, True, current)
    op = registry.operation(request.op)
    if op is None:
        return refuse('unknown_operation', request.request_id, current=current)
    if isinstance(r.validate_type(registry, op.request_ref, request.args), r.ValidationFailure):
        return refuse('invalid_args', request.request_id, current=current)
    artifact = runtime.schemas.get(current.negotiated_protocol)
    if (not runtime.ready or artifact is None
            or artifact.protocol != current.negotiated_protocol
            or artifact.digest != current.negotiated_schema_digest):
        return refuse('runtime_not_ready', request.request_id, True, current)
    if op.principal == 'seat_session' and current.binding is None:
        return refuse('not_signed_in', request.request_id, current=current)
    if ((op.principal == 'operator' and not isinstance(principal, OperatorPrincipal))
            or (op.principal == 'service' and not isinstance(principal, ServicePrincipal))):
        return refuse('not_signed_in', request.request_id, current=current)
    if request.op == 'session.resume' and current.binding is not None:
        return refuse('already_bound', request.request_id, current=current)
    try:
        handler_principal = (principal if op.principal in {'operator', 'service'}
                             else None if op.principal == 'unsigned' else current.binding)
        outcome = registry.handlers[request.op](request.args, handler_principal, operation_ports[request.op])
        if isinstance(outcome, (SessionRefusal, ComponentRefusal)):
            payload = outcome.payload if isinstance(outcome, ComponentRefusal) else None
            error = api_error(registry, outcome.cause_kind, **({} if payload is None else payload))
            if (error['code'] != outcome.code or
                    isinstance(r.validate_type(registry, op.error_ref, error), r.ValidationFailure)):
                raise ValueError('invalid_component_refusal')
            candidate = replace(current, binding=None) if (
                request.op != 'seat.identify' and error['code'] == 'E_API_AUTH'
                and outcome.cause_kind in CLEARING_AUTH_CAUSES) else current
            response = {'request_id': request.request_id, 'error': error}
            w.encode_api_response(response, registry, None, None)
            return ResponseAndConnectionState(response, candidate, False)
        candidate = current
        if isinstance(outcome, SessionOutcome):
            if request.op not in PLATFORM_SESSION_OPERATIONS:
                raise ValueError('foreign_session_outcome')
            result = outcome.result
            if outcome.binding_action == 'set' and isinstance(outcome.binding, SeatBinding):
                candidate = replace(current, binding=outcome.binding)
            elif outcome.binding_action == 'clear':
                candidate = replace(current, binding=None)
            elif outcome.binding_action != 'keep':
                raise ValueError('invalid_binding_action')
        else:
            result = outcome
        if isinstance(r.validate_operation_result(registry, request.op, result, None),
                      r.ValidationFailure):
            raise ValueError('invalid_component_result')
        response = {'request_id': request.request_id, 'result': result}
        w.encode_api_response(response, registry, request.op, None)
        return ResponseAndConnectionState(response, candidate, False)
    except Exception:
        try:
            strings.report_diagnostic(ValueError('E_API_INTERNAL'))
        except Exception:
            pass
        return refuse('handler_fault', request.request_id, current=current)
