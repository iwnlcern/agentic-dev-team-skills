"""Closed lifecycle observations, ordinary status values, and held discovery."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import Literal

from relay_engine.lifecycle_lock import HolderRecord

DownReason = Literal['ordinary', 'identity_unavailable', 'restore_blocked', 'unavailable']
_CAUSES = ('store_job_open', 'catalogue_unopenable', 'catalogue_faulted', 'start_fault', 'ingest_fault')
_STATES = ('running', 'stopped', 'down', 'not answering', 'busy')
_DOWN_REASONS = ('ordinary', 'identity_unavailable', 'restore_blocked', 'unavailable')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _text(value):
    return type(value) is str


def _optional_text(value):
    return value is None or _text(value)


def _pid(value):
    return type(value) is int and 1 <= value <= 2**64 - 1


def _texts(value):
    return type(value) is tuple and all(_text(member) for member in value)


@dataclass(frozen=True, slots=True)
class ProjectLine:
    project_id: str
    name: str
    state: Literal['served', 'stopped', 'not_served_yet']
    stopped_since: str | None

    def __post_init__(self):
        _require(_text(self.project_id) and _text(self.name) and _text(self.state)
                 and self.state in ('served', 'stopped', 'not_served_yet')
                 and _optional_text(self.stopped_since), 'invalid_project_line')


@dataclass(frozen=True, slots=True)
class RestoreNotChecked:
    kind: Literal['not_checked'] = 'not_checked'

    def __post_init__(self):
        _require(_text(self.kind) and self.kind == 'not_checked', 'invalid_restore_not_checked')


@dataclass(frozen=True, slots=True)
class RestoreClear:
    kind: Literal['clear'] = 'clear'

    def __post_init__(self):
        _require(_text(self.kind) and self.kind == 'clear', 'invalid_restore_clear')


@dataclass(frozen=True, slots=True)
class RestoreUnfinished:
    recovery_job_id: str
    backup: str
    kind: Literal['unfinished'] = field(default='unfinished', kw_only=True)

    def __post_init__(self):
        _require(_text(self.kind) and self.kind == 'unfinished'
                 and _text(self.recovery_job_id) and _text(self.backup), 'invalid_restore_unfinished')


@dataclass(frozen=True, slots=True)
class RestoreAmbiguousObservation:
    job_ids: tuple[str, ...]
    kind: Literal['ambiguous'] = field(default='ambiguous', kw_only=True)

    def __post_init__(self):
        _require(_text(self.kind) and self.kind == 'ambiguous'
                 and _texts(self.job_ids), 'invalid_restore_ambiguous')


@dataclass(frozen=True, slots=True)
class RestoreCheckFailed:
    exception_type: str
    kind: Literal['check_failed'] = field(default='check_failed', kw_only=True)

    def __post_init__(self):
        _require(_text(self.kind) and self.kind == 'check_failed'
                 and _text(self.exception_type), 'invalid_restore_check_failed')


RestoreObservation = (RestoreNotChecked | RestoreClear | RestoreUnfinished
                      | RestoreAmbiguousObservation | RestoreCheckFailed)
_RESTORE_TYPES = (RestoreNotChecked, RestoreClear, RestoreUnfinished,
                  RestoreAmbiguousObservation, RestoreCheckFailed)


def _restore(value):
    _require(type(value) in _RESTORE_TYPES, 'invalid_restore_observation')
    value.__post_init__()


@dataclass(frozen=True, slots=True)
class EngineState:
    state: Literal['running', 'stopped', 'down', 'not answering', 'busy']
    build_id: str | None
    pid: int | None
    store_state: str | None
    held_causes: tuple[str, ...]
    stopped_since: str | None
    holder: HolderRecord | None
    projects: tuple[ProjectLine, ...]
    workers: tuple[Mapping, ...]
    lines: tuple[str, ...]
    stopped_by: str | None
    mode: Literal['normal']
    normal_ready_path: str | None
    down_reason: DownReason | None
    restore_observation: RestoreObservation

    def __post_init__(self):
        _require(_text(self.state) and self.state in _STATES
                 and _text(self.mode) and self.mode == 'normal', 'invalid_normal_state')
        _require(_optional_text(self.build_id) and (self.pid is None or _pid(self.pid))
                 and _optional_text(self.store_state) and _optional_text(self.stopped_since)
                 and _optional_text(self.stopped_by) and _optional_text(self.normal_ready_path)
                 and _texts(self.lines), 'invalid_normal_member')
        _require(self.down_reason is None or (_text(self.down_reason)
                 and self.down_reason in _DOWN_REASONS), 'invalid_down_reason')
        _require(self.state == 'down' or self.down_reason is None, 'down_reason_outside_down')
        _require(_texts(self.held_causes)
                 and all(cause in _CAUSES for cause in self.held_causes)
                 and tuple(cause for cause in _CAUSES if cause in self.held_causes) == self.held_causes,
                 'invalid_held_causes')
        _require(self.store_state in (None, 'serving', 'held'), 'invalid_store_state')
        _require((self.store_state == 'held') == bool(self.held_causes), 'contradictory_store_state')
        _require(type(self.projects) is tuple
                 and all(type(project) is ProjectLine for project in self.projects), 'invalid_projects')
        for project in self.projects:
            project.__post_init__()
        _require(type(self.workers) is tuple
                 and all(isinstance(worker, Mapping) for worker in self.workers), 'invalid_workers')
        _require(self.holder is None or type(self.holder) is HolderRecord, 'invalid_holder')
        if self.holder is not None:
            self.holder.__post_init__()
        _restore(self.restore_observation)
        # Required wire fields cannot turn into fabricated text or null values.
        if self.state in ('running', 'not answering'):
            _require(_pid(self.pid), 'missing_answering_pid')
        if self.state == 'stopped':
            _require(_text(self.stopped_since) and _text(self.stopped_by), 'missing_stop_text')


@dataclass(frozen=True, slots=True)
class HeldDiagnosticObservation:
    runtime_instance_id: str
    verified_build_id: str
    verified_schema_digest: str
    held_causes: tuple[str, ...]
    kind: Literal['held_diagnostic'] = field(default='held_diagnostic', kw_only=True)
    provenance: Literal['correlated_error_probe'] = field(default='correlated_error_probe', kw_only=True)

    def __post_init__(self):
        _require(_text(self.kind) and self.kind == 'held_diagnostic'
                 and _text(self.provenance) and self.provenance == 'correlated_error_probe',
                 'invalid_diagnostic_provenance')
        _require(_text(self.runtime_instance_id) and _HEX32.fullmatch(self.runtime_instance_id)
                 and _text(self.verified_build_id) and _HEX64.fullmatch(self.verified_build_id)
                 and _text(self.verified_schema_digest) and _HEX64.fullmatch(self.verified_schema_digest)
                 and type(self.held_causes) is tuple
                 and self.held_causes == ('catalogue_unopenable',), 'invalid_diagnostic_metadata')


@dataclass(frozen=True, slots=True)
class HeldProbeOutcome:
    kind: Literal['absent', 'stale_safe', 'verified', 'invalid_unavailable', 'timed_out']
    diagnostic: HeldDiagnosticObservation | None = None

    def __post_init__(self):
        _require(_text(self.kind) and self.kind in (
            'absent', 'stale_safe', 'verified', 'invalid_unavailable', 'timed_out'),
            'invalid_held_probe_kind')
        if self.kind == 'verified':
            _require(type(self.diagnostic) is HeldDiagnosticObservation, 'missing_verified_diagnostic')
            self.diagnostic.__post_init__()
        else:
            _require(self.diagnostic is None, 'unexpected_held_diagnostic')


@dataclass(frozen=True, slots=True)
class DiagnosticEngineState:
    diagnostic: HeldDiagnosticObservation
    pid: int
    lines: tuple[str, ...]
    restore_observation: RestoreNotChecked
    mode: Literal['held_diagnostic'] = field(default='held_diagnostic', kw_only=True)
    state: Literal['running'] = field(default='running', kw_only=True)
    store_id: None = field(default=None, kw_only=True)
    store_kind: None = field(default=None, kw_only=True)
    projects: None = field(default=None, kw_only=True)
    workers: None = field(default=None, kw_only=True)
    build_id: None = field(default=None, kw_only=True)
    normal_ready_path: None = field(default=None, kw_only=True)
    down_reason: None = field(default=None, kw_only=True)
    stopped_at: None = field(default=None, kw_only=True)
    stopped_by: None = field(default=None, kw_only=True)

    def __post_init__(self):
        _require(_text(self.mode) and self.mode == 'held_diagnostic'
                 and _text(self.state) and self.state == 'running'
                 and type(self.diagnostic) is HeldDiagnosticObservation
                 and _pid(self.pid) and _texts(self.lines), 'invalid_diagnostic_state')
        self.diagnostic.__post_init__()
        _require(all(value is None for value in (self.store_id, self.store_kind, self.projects,
                 self.workers, self.build_id, self.normal_ready_path, self.down_reason,
                 self.stopped_at, self.stopped_by)), 'diagnostic_normal_metadata')
        _require(type(self.restore_observation) is RestoreNotChecked, 'diagnostic_restore_checked')
        self.restore_observation.__post_init__()


EngineObservation = EngineState | DiagnosticEngineState


def state_json(state: EngineState) -> dict:
    """Serialize supplied ordinary data; diagnostics have no ordinary value."""
    if type(state) is not EngineState:
        raise TypeError('ordinary_engine_state_required')
    # Recheck the boundary rather than coerce malformed or unknown records.
    state.__post_init__()
    running = state.state == 'running'
    stopped = state.state == 'stopped'
    holder = None
    if state.state == 'busy':
        record = state.holder
        holder = ({'kind': 'unnamed', 'name': None, 'pid': None} if record is None else
                  {'kind': 'engine' if record.holder == 'engine' else 'command',
                   'name': record.holder, 'pid': str(record.pid)})
    return {
        'state': 'not_answering' if state.state == 'not answering' else state.state,
        'pid': str(state.pid) if state.state in ('running', 'not answering') else None,
        'held_causes': list(state.held_causes) if running and state.store_state == 'held' else [],
        'stopped_at': state.stopped_since if stopped else None,
        'stopped_by': state.stopped_by if stopped else None,
        'holder': holder,
        'projects': [{'project_id': project.project_id, 'name': project.name,
                      'state': project.state, 'stopped_since': project.stopped_since}
                     for project in state.projects] if running else [],
    }


def probe_held_diagnostic(root: str, *, deadline: float | None = None) -> HeldProbeOutcome:
    """Verify one held discovery reply on the socket derived for the selected root."""
    from relay_engine.lifecycle_paths import HeldMetadataUnavailable, read_held_locator

    invalid = HeldProbeOutcome('invalid_unavailable')
    try:
        locator = read_held_locator(root)
    except (HeldMetadataUnavailable, OSError, ValueError, RuntimeError):
        return invalid
    if locator is None:
        return HeldProbeOutcome('absent')

    from errno import ECONNREFUSED, ENOENT
    from pathlib import Path
    from secrets import randbelow
    import socket
    from time import monotonic

    from relay_engine import api_definitions, api_platform, api_registry, api_schema, api_wire, jcs, version

    try:
        build = version.fingerprint(Path(__file__).resolve().parents[1])
        if locator.build_id != build:
            return invalid
        registry = api_platform.build_platform().registry
        schema = api_schema.export_schema(registry, '3.0')
    except (version.FingerprintError, OSError, ValueError):
        return invalid
    if locator.schema_digest != schema.digest:
        return invalid

    instance = locator.paths.runtime_instance_id
    request_id = str(1 + randbelow(api_definitions.MAX_UINT64))
    request = jcs.jcs_encode({
        'request_id': request_id,
        'op': 'protocol.connect',
        'args': {
            'supported': {'min': schema.protocol, 'max': schema.protocol},
            'required_build_id': build, 'required_schema_digest': schema.digest,
        },
    })
    expected_error = {
        'code': 'E_API_UNAVAILABLE',
        'class': 'unavailable',
        'cause': {
            'kind': 'held_identity_unavailable', 'runtime_instance_id': instance,
            'build_id': build, 'schema_digest': schema.digest,
        },
        'remedy': 'inspect_held_store',
    }
    socket_deadline = monotonic() + 1.0
    if deadline is not None:
        socket_deadline = min(socket_deadline, deadline)

    def remaining():
        seconds = socket_deadline - monotonic()
        if seconds <= 0:
            raise TimeoutError
        return seconds

    decoder = api_wire.ApiFrameDecoder()
    try:
        remaining()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining())
            try:
                connection.connect(locator.paths.socket_path)
            except OSError as error:
                if error.errno in (ENOENT, ECONNREFUSED):
                    return HeldProbeOutcome('stale_safe')
                raise
            connection.settimeout(remaining())
            connection.sendall(len(request).to_bytes(4, 'big') + request)
            while True:
                connection.settimeout(remaining())
                fragment = connection.recv(4096)
                remaining()
                if not fragment:
                    return invalid
                frames = decoder.feed(fragment)
                if frames:
                    break
            if len(frames) != 1:
                return invalid
            decoder.finish()
            response = api_wire.strict_json_loads(frames[0])
            if response != {'request_id': request_id, 'error': expected_error}:
                return invalid
            error = api_registry.validate_type(registry, api_definitions.TypeRef('ApiError'),
                                               response['error'])
            if isinstance(error, api_registry.ValidationFailure):
                return invalid

            # Only a valid held reply needs confirmation that the peer closed cleanly.
            connection.settimeout(remaining())
            trailing = connection.recv(1)
            remaining()
            if trailing:
                return invalid
    except TimeoutError:
        return HeldProbeOutcome('timed_out')
    except (OSError, api_wire.ApiFrameError, api_wire.StrictJsonError):
        return invalid

    return HeldProbeOutcome('verified', HeldDiagnosticObservation(
        instance, build, schema.digest, locator.held_causes))
