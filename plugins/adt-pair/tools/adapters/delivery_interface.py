"""Adapter delivery constants shared as a contract with Platform (DD-v3-adapter r8)."""

DOORBELL_POLL_INTERVAL_S: float = 5.0
NOT_HEARD_AFTER_S: float = 2 * DOORBELL_POLL_INTERVAL_S

OFF_REASONS: tuple[str, ...] = (
    "window-gone",
    "stood-down",
    "project-closed",
    "replaced",
    "version-refused",
    "session-not-current",
    "new-conversation",
)
STATES: tuple[str, ...] = ("armed", "unavailable", "off")
ARMED_REASONS: tuple[str, ...] = ("backlog",)
UNAVAILABLE_REASONS: tuple[str, ...] = ("monitor-tool",)
