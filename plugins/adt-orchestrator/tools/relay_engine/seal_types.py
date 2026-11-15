"""Public values and path-free faults of the approval seal."""
from dataclasses import dataclass
import uuid


@dataclass(frozen=True, slots=True)
class EventRow:
    project_id: str
    chain_id: str
    number: int
    kind: str
    payload: dict
    at_ms: int


@dataclass(frozen=True, slots=True)
class ReservationEntry:
    number: int
    kind: str
    subject: str
    payload: dict
    runtime_instance_id: str
    transaction: int
    position: int
    sealed: bool
    declared_kinds: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class UnreadableEntry:
    number: int
    cause: str


@dataclass(frozen=True, slots=True)
class SealedRecord:
    project_id: str
    chain_id: str
    number: int
    event: dict
    prev_mac: str | None
    key_id: str
    mac: str


@dataclass(frozen=True, slots=True)
class SealReport:
    project_id: str | None
    chain_id: str | None
    cause: str | None
    at_ms: int


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: bytes


def require_canonical_id(value: str) -> str:
    try:
        if type(value) is str and str(uuid.UUID(value)) == value:
            return value
    except (ValueError, AttributeError):
        pass
    raise ValueError('noncanonical_id')


class SealUnusable(Exception):
    def __init__(self, cause: str, chain_id: str | None):
        self.cause, self.chain_id = cause, chain_id
        super().__init__(cause, chain_id)


class ReserveRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class PointerMoved(Exception):
    def __init__(self, expected, found):
        self.expected, self.found = expected, found
        super().__init__(expected, found)


class LedgerRebindRefused(Exception):
    def __init__(self):
        super().__init__('ledger_rebind_refused')


class RecordInvalid(Exception):
    def __init__(self, cause: str):
        self.cause = cause
        super().__init__(cause)


class RecordUnreadable(Exception):
    def __init__(self, number: int, cause: str):
        self.number, self.cause = number, cause
        super().__init__(number, cause)


class SealSlotDamaged(Exception):
    def __init__(self, kind: str, project_id: str, chain_id: str | None, slot: int):
        self.kind, self.project_id, self.chain_id, self.slot = kind, project_id, chain_id, slot
        super().__init__(kind, project_id, chain_id, slot)


class SealIdentityOneSided(Exception):
    def __init__(self, project_id: str, chain_id: str | None):
        self.project_id, self.chain_id = project_id, chain_id
        super().__init__(project_id, chain_id)


class SealReportDropped(Exception):
    def __init__(self, project_id: str | None, cause: str):
        self.project_id, self.cause = project_id, cause
        super().__init__(project_id, cause)


class SealCalleeFailed(Exception):
    def __init__(self, callee: str, exc_type: str, project_id: str | None):
        self.callee, self.exc_type, self.project_id = callee, exc_type, project_id
        super().__init__(callee, exc_type, project_id)


class PointerUnreadable:
    __slots__ = ()

    def __repr__(self):
        return 'POINTER_UNREADABLE'


POINTER_UNREADABLE = PointerUnreadable()
