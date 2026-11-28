"""Store operator-item facts and exact occurrence reads."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

from relay_engine import store_steps


_JOB_KINDS = frozenset({'restore_command_started', 'report_awaiting_confirmation'})
_ITEM_QUERY = """
SELECT item.id, item.kind, item.project_id, item.subject, item.fields, ending.id
FROM store_operator_items AS item
LEFT JOIN store_operator_items AS ending ON ending.ends = item.id
"""


@dataclass(frozen=True)
class StoreItem:
    id: int
    kind: str
    project_id: str | None
    subject: str
    fields: Mapping[str, str]
    end_id: int | None


def _fields_json(fields: Mapping[str, str]) -> str:
    return json.dumps(dict(fields), sort_keys=True, separators=(',', ':'))


def write_raise(conn, *, kind: str, subject: str, project_id: str | None,
                fields: Mapping[str, str], at_ms: int) -> int:
    """Append a raise without committing the caller's transaction."""
    cursor = conn.execute(
        'INSERT INTO store_operator_items (kind, project_id, subject, at, fields) '
        'VALUES (?, ?, ?, ?, ?)', (kind, project_id, subject, at_ms, _fields_json(fields)))
    try:
        return cursor.lastrowid
    finally:
        cursor.close()


def write_end(conn, *, raise_id: int, fields: Mapping[str, str], at_ms: int) -> int:
    """Append an end only for a raise, with scope copied in the same insert."""
    cursor = conn.execute(
        'INSERT INTO store_operator_items (kind, project_id, subject, ends, at, fields) '
        'SELECT kind, project_id, subject, id, ?, ? FROM store_operator_items '
        'WHERE id = ? AND ends IS NULL', (at_ms, _fields_json(fields), raise_id))
    try:
        if cursor.rowcount != 1:
            raise ValueError(f'operator item {raise_id} is not a raise')
        return cursor.lastrowid
    finally:
        cursor.close()


def _read(conn, where, args) -> StoreItem | None:
    cursor = conn.execute(_ITEM_QUERY + where, args)
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None:
        return None
    item_id, kind, project_id, subject, fields, end_id = row
    return StoreItem(item_id, kind, project_id, subject, json.loads(fields), end_id)


def find_raise(conn, *, kind: str, subject: str, project_id: str | None) -> StoreItem | None:
    """Read a job's unique raise, possibly ended."""
    if kind not in _JOB_KINDS:
        raise ValueError(f'operator item kind is not job-unique: {kind}')
    return _read(conn,
                 'WHERE item.ends IS NULL AND item.kind = ? AND item.subject = ? '
                 'AND item.project_id IS ?', (kind, subject, project_id))


def read_item(conn, item_id: int) -> StoreItem | None:
    """Read the exact row and, for a raise, its associated end id."""
    return _read(conn, 'WHERE item.id = ?', (item_id,))


def ensure_raise(conn, *, kind: str, subject: str, project_id: str | None,
                 fields: Mapping[str, str], at_ms: int) -> int:
    """Append the first job raise or return an equal replay without writing."""
    prior = find_raise(conn, kind=kind, subject=subject, project_id=project_id)
    if prior is None:
        return write_raise(conn, kind=kind, subject=subject, project_id=project_id,
                           fields=fields, at_ms=at_ms)
    if prior.fields != fields:
        requested = {'id': prior.id, 'fields': dict(fields)}
        raise store_steps.FenceBreach(requested, requested, {'id': prior.id, 'fields': prior.fields})
    return prior.id
