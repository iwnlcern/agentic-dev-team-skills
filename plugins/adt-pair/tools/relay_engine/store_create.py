"""Delta 8: project creation and crash-safe completion through declared turns."""
from dataclasses import dataclass
from typing import Literal

from relay_engine import store_layout, store_steps, submit


@dataclass(frozen=True)
class Creation:
    """Delta 8's project id and the admission outcome of its creation."""
    project_id: str
    outcome: Literal['created', 'resumed', 'active']


class CreationHeld(Exception):
    """Delta 8 refuses a creating name reserved by a migration job."""
    def __init__(self, name: str):
        self.name = name
        super().__init__(name)


def create_project(turns: store_steps.TurnRunner, name: str, *, now_ms: int) -> Creation:
    """Delta 8 reserves a name and completes its three single-file steps."""
    def reserve(conns):
        conn = conns['catalogue']
        row = store_layout._row(
            conn, 'SELECT project_id, state, held_by_job FROM project_routes WHERE name=?',
            (name,))
        if row is None:
            project_id = submit.new_uuid7(now_ms)
            store_layout.reserve_project(conn, project_id, name, now_ms)
            return Creation(project_id, 'created')
        project_id, state, holder = row
        if state == 'active':
            return Creation(project_id, 'active')
        if holder is not None:
            raise CreationHeld(name)
        return Creation(project_id, 'resumed')

    result = turns.run(store_steps.Declared('written', {}), reserve)
    if result.outcome != 'active':
        finish_creation(turns, result.project_id)
    return result


def finish_creation(turns: store_steps.TurnRunner, project_id: str) -> None:
    """Delta 8 completes an unheld reservation from its route's payload."""
    def check(conns):
        row = store_layout._row(
            conns['catalogue'],
            'SELECT name, state, held_by_job FROM project_routes WHERE project_id=?',
            (project_id,))
        if row is None:
            raise store_layout.RouteUnavailable('not-found')
        name, state, holder = row
        if state == 'active':
            return False
        if holder is not None:
            raise CreationHeld(name)
        return True

    if not turns.run(store_steps.Declared('read', {}), check):
        return

    def insert(conns):
        row = store_layout._row(
            conns['catalogue'],
            'SELECT name, state, created_at, held_by_job FROM project_routes WHERE project_id=?',
            (project_id,))
        if row is None or row[3] is not None or row[1] not in ('creating', 'active'):
            raise store_layout.ReservationChanged('creation reservation changed')
        conns[project_id].execute(
            'INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?) '
            'ON CONFLICT(id) DO NOTHING', (project_id, row[0], row[2])).close()

    turns.run(store_steps.Declared('read', {project_id: 'created'}), insert)
    turns.run(store_steps.Declared('written', {}),
              lambda conns: store_layout.activate_project(conns['catalogue'], project_id))
