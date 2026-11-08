"""Two guarded unsigned GET translations against an explicitly selected backend."""
from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
from typing import Protocol
from urllib.parse import urlsplit

from relay_engine import api_definitions as d, api_protocol as p, api_registry as r, api_wire as w, jcs


class BackendConnection(Protocol):
    def exchange(self, frame: bytes) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    method: str
    path: str
    raw_query: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class BridgeContext:
    advertised_host: str
    advertised_origin: str
    open_backend: Callable[[], BackendConnection]


@dataclass(frozen=True, slots=True)
class CoreDiscoveryExchange:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _loopback_context(context):
    try:
        origin = urlsplit(context.advertised_origin)
        if (origin.scheme not in {'http', 'https'} or origin.netloc != context.advertised_host or
                origin.path or origin.query or origin.fragment or origin.username is not None or
                origin.password is not None or not origin.hostname):
            return False
        if origin.port is not None and not 1 <= origin.port <= 65535:
            return False
        return origin.hostname == 'localhost' or ipaddress.ip_address(origin.hostname).is_loopback
    except ValueError:
        return False


def _guard(request, context):
    if request.method != 'GET' or request.body != b'' or not _loopback_context(context):
        return False
    hosts, origins = [], []
    for name, value in request.headers:
        if name.lower() == 'host':
            hosts.append(value)
        elif name.lower() == 'origin':
            origins.append(value)
    return hosts == [context.advertised_host] and (not origins or origins == [context.advertised_origin])


def _query(raw):
    # All values in this profile have an unescaped ASCII spelling; do not normalize aliases.
    if not isinstance(raw, str) or not raw or not raw.isascii() or '%' in raw or '+' in raw:
        raise ValueError('invalid_query')
    values = {}
    for pair in raw.split('&'):
        if pair.count('=') != 1:
            raise ValueError('invalid_query')
        name, value = pair.split('=')
        if not name or not value or name in values:
            raise ValueError('invalid_query')
        values[name] = value
    return values


def _frame_request(request_id, op, args):
    body = jcs.jcs_encode({'request_id': request_id, 'op': op, 'args': args})
    if not 1 <= len(body) <= w.MAX_API_FRAME:
        raise ValueError('invalid_request')
    return len(body).to_bytes(4, 'big') + body


def _backend_response(backend, registry, request_id, op, args, context=None):
    raw = backend.exchange(_frame_request(request_id, op, args))
    decoder = w.ApiFrameDecoder()
    bodies = decoder.feed(raw)
    decoder.finish()
    if len(bodies) != 1:
        raise ValueError('invalid_backend_response')
    response = w.strict_json_loads(bodies[0])
    if type(response) is not dict or response.get('request_id') != request_id:
        raise ValueError('invalid_backend_response')
    w.encode_api_response(response, registry, op, context)
    return response


def _checked_connect(response, args):
    if 'error' in response:
        return None
    result = response['result']
    protocol = p.negotiate_protocol(args['supported'])
    if protocol is None or result['protocol'] != protocol:
        raise ValueError('invalid_backend_response')
    for field in ('build_id', 'schema_digest'):
        pin = args.get('required_' + field)
        if pin is not None and result[field] != pin:
            raise ValueError('invalid_backend_response')
    return r.DiscoveryValidationContext('schema_reply', result['protocol'], result['schema_digest'])


def map_discovery_get(request: BridgeRequest, bridge_context: BridgeContext) -> CoreDiscoveryExchange:
    registry = p.discovery_registry()
    request_id = {'/api/connect': '1', '/api/schema': '2'}.get(request.path)

    def answer(response):
        # Full error arms were checked above or assembled from the authoritative catalogue.
        status = 200 if 'result' in response else {
            'wire': 400, 'version': 409, 'unavailable': 503, 'internal': 500}[response['error']['class']]
        return CoreDiscoveryExchange(status, (('Content-Type', 'application/json; charset=utf-8'),
                                               ('Cache-Control', 'no-store')), jcs.jcs_encode(response))

    def refuse(kind):
        return answer({'request_id': request_id, 'error': p.api_error(registry, kind)})

    if request_id is None or not _guard(request, bridge_context):
        return refuse('invalid_envelope')
    try:
        query = _query(request.raw_query)
        if request.path == '/api/connect':
            required = {'min_protocol', 'max_protocol'}
            if not required <= query.keys() or query.keys() - required - {'required_build_id', 'required_schema_digest'}:
                raise ValueError('invalid_query')
            args = {'supported': {'min': query['min_protocol'], 'max': query['max_protocol']}}
            args.update({name: value for name, value in query.items() if name not in required})
        else:
            if set(query) != {'protocol', 'required_build_id', 'required_schema_digest'}:
                raise ValueError('invalid_query')
            args = {'supported': {'min': query['protocol'], 'max': query['protocol']},
                    'required_build_id': query['required_build_id'],
                    'required_schema_digest': query['required_schema_digest']}
        if isinstance(r.validate_type(registry, d.TypeRef('ConnectArgs'), args), r.ValidationFailure):
            raise ValueError('invalid_query')
    except ValueError:
        return refuse('invalid_args')
    backend = None
    try:
        backend = bridge_context.open_backend()
        response = _backend_response(backend, registry, '1', 'protocol.connect', args)
        context = _checked_connect(response, args)
        if request.path == '/api/schema' and context is not None:
            response = _backend_response(backend, registry, '2', 'protocol.schema', {}, context)
        return answer(dict(response, request_id=request_id))
    except Exception:
        return refuse('runtime_not_ready')
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
