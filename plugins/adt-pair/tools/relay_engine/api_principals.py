"""Frozen operator and service principal types with closed value lists.

Nothing outside tests constructs these until Platform's operator-authentication
and service-principal work adds the transport and supervision paths that do.
"""
from dataclasses import dataclass

OPERATOR_SURFACES: tuple[str, ...] = ("frank_panel", "page")
SERVICE_NAMES: tuple[str, ...] = (
    "authority_service",
    "console_queue",
    "engine_liveness",
    "xproject_transfer",
)


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    surface: str

    def __post_init__(self):
        if type(self.surface) is not str or self.surface not in OPERATOR_SURFACES:
            raise ValueError("invalid_operator_surface")


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    service: str

    def __post_init__(self):
        if type(self.service) is not str or self.service not in SERVICE_NAMES:
            raise ValueError("invalid_service_principal")
