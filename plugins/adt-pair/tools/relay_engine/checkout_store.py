"""Checkout jobs for caller-owned catalogue and ledger transactions."""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from relay_engine import submit
from relay_engine.store_layout import CheckoutRoute


@dataclass(frozen=True)
class RepositoryEnsured:
    repository_id: str
    ledger_order: int
    inserted: bool


@dataclass(frozen=True)
class NewRoute:
    checkout_id: str
    project_id: str
    repository_id: str
    git_common_dir: str
    worktree_path: str
    dev: int
    ino: int


@dataclass(frozen=True)
class Registered:
    created_at: int


@dataclass(frozen=True)
class AlreadyRegistered:
    pass


@dataclass(frozen=True)
class Ended:
    checkout_id: str
    end_reason: str


@dataclass(frozen=True)
class NotRegistered:
    pass


class CheckoutRefused(Exception):
    """A checkout request refused before its job writes anything."""

    def __init__(self, reason: str, detail: Mapping[str, str]):
        self.reason = reason
        self.detail = dict(detail)
        super().__init__(reason)


def _one(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _changed(conn, sql, parameters):
    cursor = conn.execute(sql, parameters)
    try:
        return cursor.rowcount == 1
    finally:
        cursor.close()


def ensure_repository_step(conn, project_id: str, name: str, *, now_ms: int) -> RepositoryEnsured:
    """Find or insert the repository and read the ledger's global order floor."""
    row = _one(conn, 'SELECT id FROM repositories WHERE project_id=? AND name=?',
               (project_id, name))
    inserted = row is None
    if inserted:
        repository_id = submit.new_uuid7(now_ms)
        conn.execute('INSERT INTO repositories (id, project_id, name) VALUES (?, ?, ?)',
                     (repository_id, project_id, name)).close()
    else:
        repository_id = row[0]
    ledger_order = _one(conn, 'SELECT COALESCE(MAX(route_created_at), 0) '
                        'FROM repository_checkouts')[0]
    return RepositoryEnsured(repository_id, ledger_order, inserted)


def register_route_step(conn, route: NewRoute, *, ledger_order: int,
                        now_ms: int) -> Registered | AlreadyRegistered:
    """Register a free inode, refusing mismatched ownership without mutation."""
    project = _one(conn, 'SELECT state FROM project_routes WHERE project_id=?',
                   (route.project_id,))
    if project is None:
        raise CheckoutRefused('project_not_found', {'project_id': route.project_id})
    if project[0] != 'active':
        raise CheckoutRefused('project_not_active', {'project_id': route.project_id})
    open_row = _one(conn, 'SELECT checkout_id, project_id, repository_id '
                    'FROM checkout_routes WHERE dev=? AND ino=? AND ended_at IS NULL',
                    (route.dev, route.ino))
    if open_row is not None:
        if open_row == (route.checkout_id, route.project_id, route.repository_id):
            return AlreadyRegistered()
        detail = {'project_id': open_row[1], 'repository_id': open_row[2]}
        if open_row[1:] != (route.project_id, route.repository_id):
            raise CheckoutRefused('checkout_registered_elsewhere', detail)
        raise CheckoutRefused('checkout_route_unmatched', detail)
    catalogue_order = _one(conn, 'SELECT COALESCE(MAX(created_at), 0) FROM checkout_routes')[0]
    created_at = max(now_ms, catalogue_order + 1, ledger_order + 1)
    conn.execute('INSERT INTO checkout_routes '
                 '(checkout_id, project_id, repository_id, git_common_dir, worktree_path, '
                 'dev, ino, created_at, ended_at, end_reason, ledger_synced_at) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)',
                 (route.checkout_id, route.project_id, route.repository_id, route.git_common_dir,
                  route.worktree_path, route.dev, route.ino, created_at)).close()
    return Registered(created_at)


def unregister_route_step(conn, *, dev: int, ino: int, checkout_id: str | None,
                          now_ms: int) -> Ended | NotRegistered:
    """End the inode's current route, even when the retained id does not match."""
    row = _one(conn, 'SELECT checkout_id FROM checkout_routes '
               'WHERE dev=? AND ino=? AND ended_at IS NULL', (dev, ino))
    if row is None:
        return NotRegistered()
    reason = 'matched' if checkout_id == row[0] else 'unmatched'
    conn.execute('UPDATE checkout_routes SET ended_at=?, end_reason=?, ledger_synced_at=NULL '
                 'WHERE checkout_id=?', (now_ms, reason, row[0])).close()
    return Ended(row[0], reason)


def write_ledger_row_step(conn, route: CheckoutRoute, *, now_ms: int) -> Literal['written', 'superseded']:
    """Write a newer owner or refresh its open row; never revive its ended row."""
    changed = _changed(conn, 'INSERT INTO repository_checkouts '
                       '(repository_id, path, last_seen_at, checkout_id, route_created_at) '
                       'VALUES (?, ?, ?, ?, ?) ON CONFLICT (path) DO UPDATE SET '
                       'repository_id=excluded.repository_id, last_seen_at=excluded.last_seen_at, '
                       'checkout_id=excluded.checkout_id, route_created_at=excluded.route_created_at, '
                       'ended_at=NULL WHERE '
                       'excluded.route_created_at > repository_checkouts.route_created_at OR '
                       '(excluded.checkout_id = repository_checkouts.checkout_id '
                       'AND repository_checkouts.ended_at IS NULL)',
                       (route.repository_id, route.worktree_path, now_ms,
                        route.checkout_id, route.created_at))
    return 'written' if changed else 'superseded'


def end_ledger_row_step(conn, route: CheckoutRoute, *, now_ms: int) -> Literal['ended', 'unchanged']:
    """End only the open ledger row still owned by this route."""
    changed = _changed(conn, 'UPDATE repository_checkouts SET ended_at=? '
                       'WHERE path=? AND checkout_id=? AND ended_at IS NULL',
                       (now_ms, route.worktree_path, route.checkout_id))
    return 'ended' if changed else 'unchanged'


def acknowledge_step(conn, checkout_id: str, owed: Literal['write', 'end'], *, now_ms: int) -> bool:
    """Set the marker only while the route still owes the completed kind of work."""
    return _changed(conn, 'UPDATE checkout_routes SET ledger_synced_at=? '
                    'WHERE checkout_id=? AND ledger_synced_at IS NULL AND (ended_at IS NULL) = ?',
                    (now_ms, checkout_id, owed == 'write'))
