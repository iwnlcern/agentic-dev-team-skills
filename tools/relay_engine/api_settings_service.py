"""Store-backed project settings operations for the Platform component."""

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import Protocol

from relay_engine import errors, queries, store_layout
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.api_protocol import ComponentRefusal
from relay_engine.api_settings import INTERVAL, THRESHOLD, _order
from relay_engine.api_submit import ServiceContext
from relay_engine.api_writer import JobOutcome, WriterUnavailable


_PROJECT_UNKNOWN = ComponentRefusal('E_API_NOT_FOUND', 'project_unknown')
_NOT_READY = ComponentRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
_INVALID_ARGS = ComponentRefusal('E_API_PROTOCOL', 'invalid_args')
_STALE = ComponentRefusal('E_API_CONFLICT', 'settings_revision_stale')
_HANDLER_FAULT = ComponentRefusal('E_API_INTERNAL', 'handler_fault')


class SettingsPort(Protocol):
    def get(self, args: Mapping) -> Mapping | ComponentRefusal: ...

    def set(self, args: Mapping, principal: OperatorPrincipal) -> Mapping | ComponentRefusal: ...


def _record(project_id, current):
    return {'project_id': project_id, 'revision': str(current.revision),
            THRESHOLD: str(current.values[THRESHOLD]), INTERVAL: str(current.values[INTERVAL])}


def _read_snapshot(conn, project_id):
    conn.execute('BEGIN').close()
    try:
        return _record(project_id, queries.project_settings(conn, project_id))
    finally:
        conn.execute('ROLLBACK').close()


@dataclass(frozen=True)
class _SettingsJob:
    files: frozenset[str]
    project_id: str
    expected_revision: int
    values: Mapping[str, int]
    now_ms: int

    def run(self, conn) -> JobOutcome:
        current = queries.project_settings(conn, self.project_id)
        keys = _order(current.values, self.values)
        if not keys:
            return JobOutcome(False, _record(self.project_id, current))
        if current.revision != self.expected_revision:
            return JobOutcome(False, _STALE)
        try:
            for key in keys:
                queries.set_project_setting_step(conn, self.project_id, key, self.values[key],
                                                 principal='operator', now_ms=self.now_ms)
        except errors.EngineError as error:
            return JobOutcome(False, ComponentRefusal(
                'E_API_ADMISSION', error.cls, {'engine_code': error.code}))
        return JobOutcome(True, _record(self.project_id, queries.project_settings(conn, self.project_id)))


class SettingsService:
    def __init__(self, context: ServiceContext):
        self.context = context

    def _project_refusal(self, project_id: str) -> ComponentRefusal | None:
        with closing(store_layout.open_catalogue_readonly(self.context.store_root)) as catalogue:
            try:
                store_layout.route(catalogue, project_id=project_id)
            except store_layout.RouteUnavailable as error:
                if error.cause == 'not-found':
                    return _PROJECT_UNKNOWN
                if error.cause == 'creating':
                    return _NOT_READY
                raise
            except ValueError:
                return _PROJECT_UNKNOWN
        if project_id not in self.context.port.served_projects():
            return _NOT_READY
        return None

    def get(self, args: Mapping) -> Mapping | ComponentRefusal:
        project_id = args['project_id']
        try:
            refusal = self._project_refusal(project_id)
            if refusal is not None:
                return refusal
            return self.context.port.read(project_id, lambda conn: _read_snapshot(conn, project_id))
        except WriterUnavailable:
            return _NOT_READY

    def set(self, args: Mapping, principal: OperatorPrincipal) -> Mapping | ComponentRefusal:
        project_id = args['project_id']
        values = {key: int(args[key]) for key in (THRESHOLD, INTERVAL) if key in args}
        if not values:
            return _INVALID_ARGS
        try:
            refusal = self._project_refusal(project_id)
            if refusal is not None:
                return refusal
            port = self.context.port
            files = frozenset({port.ledger_file(project_id)})
            outcome = port.submit(project_id, _SettingsJob(
                files, project_id, int(args['expected_revision']), values, self.context.clock()))
        except WriterUnavailable:
            return _NOT_READY
        if outcome.indeterminate or outcome.fault is not None:
            return _HANDLER_FAULT
        return outcome.value
