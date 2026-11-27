"""Generator-safe project settings definitions and component handlers."""

from collections.abc import Callable, Mapping

from relay_engine import api_definitions as d


THRESHOLD = 'doorbell_off_minutes'
INTERVAL = 'delivery_interval_seconds'


def settings_definitions() -> d.DefinitionGraph:
    ref, field = d.TypeRef, d.FieldDef
    project = field('project_id', ref('OpaqueId'))
    integer = ref('UInt64')
    error = ref('ApiError')
    types = (
        d.TypeDef('ProjectSettingsRecord', d.Record((
            project, field('revision', integer), field(THRESHOLD, integer),
            field(INTERVAL, integer),
        ))),
        d.TypeDef('ProjectSettingsGetArgs', d.Record((project,))),
        d.TypeDef('ProjectSettingsSetArgs', d.Record((
            project, field('expected_revision', integer),
            field(THRESHOLD, integer, optional=True), field(INTERVAL, integer, optional=True),
        ))),
    )
    result = ref('ProjectSettingsRecord')
    operations = (
        d.OperationDef('project.settings.get', ref('ProjectSettingsGetArgs'), result,
                       error, 'unsigned', 'pure_observation', 'read_repeat', 'none'),
        d.OperationDef('project.settings.set', ref('ProjectSettingsSetArgs'), result,
                       error, 'operator', 'settings_write', 'read_repeat', 'none'),
    )
    return d.DefinitionGraph(types, operations, error)


def _order(current: Mapping[str, int], sent: Mapping[str, int]) -> tuple[str, ...]:
    rising = sent.get(THRESHOLD, current[THRESHOLD]) > current[THRESHOLD]
    order = (THRESHOLD, INTERVAL) if rising else (INTERVAL, THRESHOLD)
    return tuple(key for key in order if key in sent and sent[key] != current[key])


def _get(args, _principal, port):
    return port.settings.get(args)


def _set(args, principal, port):
    return port.settings.set(args, principal)


def settings_handlers() -> Mapping[str, Callable]:
    return {'project.settings.get': _get, 'project.settings.set': _set}
