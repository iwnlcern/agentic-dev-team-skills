"""Generator-safe assembly of the actual discovery and session handlers."""
from collections.abc import Callable, Mapping

from relay_engine import api_definitions, api_protocol, api_registry, errors


def platform_handlers() -> Mapping[str, Callable]:
    handlers = api_protocol.discovery_handlers()
    additions = api_protocol.session_handlers()
    if handlers.keys() & additions.keys():
        raise ValueError('overlapping_handler')
    return dict(handlers, **additions)


def build_platform_registry() -> api_registry.FrozenApiRegistry:
    return api_registry.compile_registry(api_definitions.platform_definitions(),
        platform_handlers(), (errors.api_error_catalog(),))
