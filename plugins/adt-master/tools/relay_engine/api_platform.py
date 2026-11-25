"""Generator-safe Platform component assembly with deferred runtime services."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from relay_engine import api_compose, api_definitions, api_protocol, api_registry, api_relay_reads, errors


@dataclass(frozen=True)
class PlatformPort:
    sessions: api_protocol.SessionPort
    submit: api_protocol.SubmitPort
    reads: api_relay_reads.RelayReadsPort


@dataclass(frozen=True)
class PlatformBuild:
    registry: api_registry.FrozenApiRegistry
    composition: api_compose.Composition


def platform_handlers() -> Mapping[str, Callable]:
    handlers = {}
    for additions in (api_protocol.discovery_handlers(), api_protocol.session_handlers(),
                      api_protocol.submit_handlers(), api_relay_reads.reads_handlers()):
        if handlers.keys() & additions.keys():
            raise ValueError('overlapping_handler')
        handlers.update(additions)
    return handlers


def _platform_port(context):
    from relay_engine import api_relay_reads_service, api_sessions, api_submit
    return PlatformPort(api_sessions.SessionService(context.port, context.clock,
        project_id=context.project_id), api_submit.SubmitService(context),
        api_relay_reads_service.RelayReadsService(context))


def platform_component() -> api_compose.ApiComponent:
    base = api_definitions.platform_definitions()
    reads = api_relay_reads.reads_definitions()
    definitions = replace(base, types=base.types + reads.types,
                          operations=base.operations + reads.operations)
    return api_compose.ApiComponent('platform', definitions, platform_handlers(), _platform_port)


def platform_components() -> tuple[api_compose.ApiComponent, ...]:
    from relay_engine.operator_queue.component import queue_component

    return (platform_component(), queue_component())


def build_platform() -> PlatformBuild:
    composition = api_compose.compose_components(platform_components())
    registry = api_registry.compile_registry(composition.definitions, composition.handlers,
                                             (errors.api_error_catalog(),))
    return PlatformBuild(registry, composition)
