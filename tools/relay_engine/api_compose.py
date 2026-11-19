"""Dependency-neutral composition of component definitions and owner routing."""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .api_definitions import DefinitionGraph, TypeRef


@dataclass(frozen=True, slots=True)
class ApiComponent:
    owner: str
    definitions: DefinitionGraph
    handlers: Mapping[str, Callable]
    port_factory: Callable[["ServiceContext"], object] | None


@dataclass(frozen=True, slots=True)
class Composition:
    definitions: DefinitionGraph
    handlers: Mapping[str, Callable]
    owners: Mapping[str, str]
    port_factories: Mapping[str, Callable]

    def __post_init__(self):
        object.__setattr__(self, 'handlers', MappingProxyType(dict(self.handlers)))
        object.__setattr__(self, 'owners', MappingProxyType(dict(self.owners)))
        object.__setattr__(self, 'port_factories', MappingProxyType(dict(self.port_factories)))


class CompositionError(ValueError):
    def __init__(self, reason):
        if reason not in {'duplicate_owner', 'duplicate_type', 'duplicate_operation',
                          'undefined_handler', 'missing_port_factory'}:
            raise ValueError('unknown composition reason')
        self.reason = reason
        super().__init__(reason)


def compose_components(components: Sequence[ApiComponent]) -> Composition:
    owners_seen = set()
    types = {}
    operations = {}
    handlers = {}
    owners = {}
    factories = {}
    for component in components:
        if component.owner in owners_seen:
            raise CompositionError('duplicate_owner')
        owners_seen.add(component.owner)
        for node in component.definitions.types:
            if node.name in types:
                raise CompositionError('duplicate_type')
            types[node.name] = node
        local_operations = set()
        for operation in component.definitions.operations:
            if operation.name in operations:
                raise CompositionError('duplicate_operation')
            operations[operation.name] = operation
            owners[operation.name] = component.owner
            local_operations.add(operation.name)
        if any(name not in local_operations for name in component.handlers):
            raise CompositionError('undefined_handler')
        if local_operations and component.port_factory is None:
            raise CompositionError('missing_port_factory')
        handlers.update(component.handlers)
        if component.port_factory is not None:
            factories[component.owner] = component.port_factory
    graph = DefinitionGraph(tuple(types.values()), tuple(operations.values()), TypeRef('ApiError'))
    return Composition(graph, handlers, owners, factories)
