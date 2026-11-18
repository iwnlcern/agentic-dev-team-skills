"""Platform's noncommitting step that writes a moved project's identity rows.

Store's slice 2a import calls `import_identities_step` once inside its batch
transaction, before any relay.  The step checks every input before its first
write, then writes `projects`, `team_instances` and `seats` inside one
savepoint of the caller's transaction, and never commits, rolls back the
caller's work or begins a transaction of its own.
"""
import dataclasses
import sqlite3
import types
import uuid
from collections.abc import Mapping, Sequence

from relay_engine import addresses, schema

# The role of each level's top seat (DD-v3-platform identity import r2 section 5).
TOP_SEAT_ROLES: Mapping[str, str] = types.MappingProxyType({
    "master": "master-planner",
    "domain": "domain-planner",
    "orchestrator": "orchestrator-planner",
    "pair": "planner",
})

_SAVEPOINT = "identity_import"


class IdentityImportRefused(ValueError):
    """A refusal with one of the fixed codes; nothing was written."""

    def __init__(self, reason, item=None):
        super().__init__(reason)
        self.reason = reason
        self.item = item


@dataclasses.dataclass(frozen=True, slots=True)
class ImportedTeamInstance:
    id: str
    name: str
    level: str
    parent_instance_id: str | None
    opened_at: int
    closed_at: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class ImportedSeat:
    id: str
    team_instance_id: str
    role: str


def _valid_id(value):
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.version == 7


def _valid_time(value):
    return type(value) is int and value >= 0


def _check_name(kind, value):
    if type(value) is not str:
        raise IdentityImportRefused("invalid_name", value)
    try:
        addresses.validate_name(kind, value)
    except addresses.AddressRefused:
        raise IdentityImportRefused("invalid_name", value) from None


def _check_identities(instances, seats, other_ids):
    """Section 4's checks on instances and seats, in the table's order."""
    given = list(other_ids)
    given.extend(instance.id for instance in instances)
    given.extend(seat.id for seat in seats)
    seen = set()
    for value in given:
        if not _valid_id(value) or value in seen:
            raise IdentityImportRefused("invalid_id", value)
        seen.add(value)
    for instance in instances:
        parent = instance.parent_instance_id
        if parent is not None and not _valid_id(parent):
            raise IdentityImportRefused("invalid_id", parent)
    for seat in seats:
        if not _valid_id(seat.team_instance_id):
            raise IdentityImportRefused("invalid_id", seat.team_instance_id)
    for instance in instances:
        _check_name("team", instance.name)
    for seat in seats:
        role = seat.role
        if type(role) is not str or role not in addresses.ROLE_WORDS:
            if type(role) is str and role in addresses.RETIRED_ROLE_ALIASES:
                raise IdentityImportRefused("invalid_role", addresses.RETIRED_ROLE_ALIASES[role])
            raise IdentityImportRefused("invalid_role", role)
    for instance in instances:
        if type(instance.level) is not str or instance.level not in schema._LEVELS:
            raise IdentityImportRefused("invalid_level", instance.level)
    for instance in instances:
        if not _valid_time(instance.opened_at):
            raise IdentityImportRefused("invalid_time", instance.opened_at)
        closed = instance.closed_at
        if closed is not None and (not _valid_time(closed) or closed < instance.opened_at):
            raise IdentityImportRefused("invalid_time", closed)
    by_id = {instance.id: instance for instance in instances}
    for instance in instances:
        parent = instance.parent_instance_id
        if parent is not None and parent not in by_id:
            raise IdentityImportRefused("unknown_parent", parent)
    for instance in instances:
        visited = {instance.id}
        current = instance
        while current.parent_instance_id is not None:
            if current.parent_instance_id in visited:
                raise IdentityImportRefused("parent_cycle", instance.id)
            visited.add(current.parent_instance_id)
            current = by_id[current.parent_instance_id]
    open_names = set()
    for instance in instances:
        if instance.closed_at is None:
            if instance.name in open_names:
                raise IdentityImportRefused("open_name_duplicate", instance.name)
            open_names.add(instance.name)
    places = set()
    for seat in seats:
        if seat.team_instance_id not in by_id:
            raise IdentityImportRefused("unknown_instance", seat.team_instance_id)
        place = (seat.team_instance_id, seat.role)
        if place in places:
            raise IdentityImportRefused("duplicate_seat", seat.id)
        places.add(place)


def _top_seat(instances, seats):
    open_instances = [instance for instance in instances if instance.closed_at is None]
    masters = [instance for instance in open_instances if instance.level == "master"]
    if len(masters) > 1:
        raise IdentityImportRefused("top_seat_unresolved")
    if masters:
        chosen = masters[0]
    elif len(open_instances) > 1:
        raise IdentityImportRefused("top_seat_unresolved")
    elif open_instances:
        chosen = open_instances[0]
    else:
        return None
    role = TOP_SEAT_ROLES[chosen.level]
    for seat in seats:
        if seat.team_instance_id == chosen.id and seat.role == role:
            return seat.id
    raise IdentityImportRefused("top_seat_unresolved", chosen.id)


def derive_top_seat(instances: Sequence[ImportedTeamInstance],
                    seats: Sequence[ImportedSeat]) -> str | None:
    """Answer the seat id section 5 selects, or None; refuse malformed input."""
    instances = tuple(instances)
    seats = tuple(seats)
    _check_identities(instances, seats, ())
    return _top_seat(instances, seats)


def _parents_first(instances):
    by_id = {instance.id: instance for instance in instances}

    def depth(instance):
        count = 0
        while instance.parent_instance_id is not None:
            instance = by_id[instance.parent_instance_id]
            count += 1
        return count

    return sorted(instances, key=depth)


def import_identities_step(conn: sqlite3.Connection, *, project_id: str, project_name: str, created_at: int,
                           instances: Sequence[ImportedTeamInstance],
                           seats: Sequence[ImportedSeat],
                           top_seat_id: str | None) -> None:
    """Write the project row, its team instances and seats; never commit."""
    if not conn.in_transaction:
        raise ValueError("transaction_required")
    instances = tuple(instances)
    seats = tuple(seats)
    if conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None:
        raise IdentityImportRefused("project_exists", project_id)
    _check_identities(instances, seats, (project_id,))
    _check_name("project", project_name)
    if not _valid_time(created_at):
        raise IdentityImportRefused("invalid_time", created_at)
    if top_seat_id != _top_seat(instances, seats):
        raise IdentityImportRefused("top_seat_mismatch", top_seat_id)
    open_ids = {instance.id for instance in instances if instance.closed_at is None}
    conn.execute("SAVEPOINT " + _SAVEPOINT)
    try:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, top_seat_id) VALUES (?, ?, ?, NULL)",
            (project_id, project_name, created_at))
        for instance in _parents_first(instances):
            conn.execute(
                "INSERT INTO team_instances (id, project_id, name, level, parent_instance_id,"
                " opened_at, opened_by, closed_at) VALUES (?, ?, ?, ?, ?, ?, 'operator', ?)",
                (instance.id, project_id, instance.name, instance.level,
                 instance.parent_instance_id, instance.opened_at, instance.closed_at))
        for seat in seats:
            state = "vacant" if seat.team_instance_id in open_ids else "stood-down"
            conn.execute(
                "INSERT INTO seats (id, team_instance_id, role, state) VALUES (?, ?, ?, ?)",
                (seat.id, seat.team_instance_id, seat.role, state))
        conn.execute("UPDATE projects SET top_seat_id = ? WHERE id = ?", (top_seat_id, project_id))
        conn.execute("RELEASE " + _SAVEPOINT)
    except BaseException:
        conn.execute("ROLLBACK TO " + _SAVEPOINT)
        conn.execute("RELEASE " + _SAVEPOINT)
        raise
