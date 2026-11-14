"""Operator notice actions and pure reads reconstructed at an event high-water."""
import base64
from dataclasses import dataclass
import json
import re

from relay_engine.api_principals import OperatorPrincipal
from relay_engine.api_sessions import request_digest
from relay_engine.jcs import jcs_encode
from relay_engine.operator_queue import kinds, records


@dataclass(frozen=True)
class IngestSkip:
    source: str
    row_ref: str
    reason: str
    skipped_at_ms: int


def _dicts(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    try:
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _refusal(cause, *, code='E_QUEUE_STATE', payload=None):
    return records.QueueRefusal(code, cause, payload)


def _replay(conn, principal, operation, operation_key, args):
    replay = records.replay_or_none(conn, principal, operation, operation_key, args)
    if replay is None or isinstance(replay, records.QueueRefusal):
        return replay
    result = dict(replay)
    result['receipt'] = result.pop('receipt_id')
    return result


def _complete(conn, principal, operation, operation_key, args, result, now_ms):
    receipt = records.record_receipt(conn, principal, operation, operation_key, args, result, now_ms)
    return dict(result, receipt=receipt, replayed=False)


def _operator(principal):
    return isinstance(principal, OperatorPrincipal)


def _move(conn, principal, item, event_kind, now_ms):
    revision = item['revision'] + 1
    records._write(conn, "UPDATE queue_items SET state='dismissed',revision=? WHERE item_id=?",
                   (revision, item['item_id']))
    records.append_event(conn, item['item_id'], revision, 'dismissed', event_kind, principal, now_ms)
    if item['subkind'] in kinds.OPERATOR_CLOSED_SUBKINDS:
        condition = records._one(conn, 'SELECT closed_at_ms FROM queue_conditions WHERE item_id=?',
                                 (item['item_id'],))
        if condition is not None and condition[0] is None:
            records._close_condition(conn, item['item_id'], revision, 'dismissed', principal, now_ms)
    return dict(item_id=item['item_id'], revision=revision, state='dismissed')


def _notice_action(conn, principal, item_id, expected_revision, operation_key, now_ms, *, opened):
    if not _operator(principal):
        return _refusal('not_operator', code='E_API_AUTH')
    operation = 'queue.open' if opened else 'queue.dismiss'
    args = dict(item_id=item_id, expected_revision=expected_revision)
    replay = _replay(conn, principal, operation, operation_key, args)
    if replay is not None:
        return replay
    rows = _dicts(conn, 'SELECT * FROM queue_items WHERE item_id=?', (item_id,))
    if not rows:
        return _refusal('item_not_found')
    item = rows[0]
    if item['kind'] != 'notice' and not (not opened and item['kind'] == 'question'
                                         and item['answer_to'] is None):
        return _refusal('not_a_notice', code='E_QUEUE_INVALID')
    if item['state'] != 'pending':
        return _refusal('item_not_pending')
    if item['revision'] != expected_revision:
        return _refusal('revision_mismatch', payload={'current_revision': item['revision'],
                                                    'current_state': item['state']})
    result = _move(conn, principal, item, 'opened' if opened else 'dismissed', now_ms)
    return _complete(conn, principal, operation, operation_key, args, result, now_ms)


def dismiss(conn, principal, item_id, expected_revision, operation_key, now_ms):
    return _notice_action(conn, principal, item_id, expected_revision, operation_key, now_ms, opened=False)


def open_notice(conn, principal, item_id, expected_revision, operation_key, now_ms):
    return _notice_action(conn, principal, item_id, expected_revision, operation_key, now_ms, opened=True)


def dismiss_all(conn, principal, scope: str, up_to_position: int, operation_key: str, now_ms):
    if not _operator(principal):
        return _refusal('not_operator', code='E_API_AUTH')
    args = dict(scope=scope, up_to_event_position=up_to_position)
    replay = _replay(conn, principal, 'queue.dismiss_all', operation_key, args)
    if replay is not None:
        return replay
    items = _dicts(conn, "SELECT * FROM queue_items WHERE kind='notice' AND state='pending' "
                   "AND raised_position<=? AND (?='machine' OR project_id=?) "
                   'ORDER BY raised_position,item_id LIMIT 1001', (up_to_position, scope, scope))
    selected = items[:1000]
    for item in selected:
        _move(conn, principal, item, 'dismissed_all', now_ms)
    result = dict(count=len(selected), item_ids=[item['item_id'] for item in selected],
                  more_remaining=len(items) > 1000)
    return _complete(conn, principal, 'queue.dismiss_all', operation_key, args, result, now_ms)


def _page_size(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError('page size must be an integer from 1 to 1000')


def _filters(value, allowed):
    if value is None:
        return None
    values = [value] if isinstance(value, str) else list(value)
    if any(not isinstance(member, str) or member not in allowed for member in values):
        raise ValueError('invalid queue filter')
    return sorted(set(values))


def _encode_token(value):
    return base64.urlsafe_b64encode(jcs_encode(value)).rstrip(b'=').decode('ascii')


def _decode_token(token, identity, family, scope, selector, maximum):
    if not isinstance(token, str) or re.fullmatch('[A-Za-z0-9_-]+', token) is None:
        return None
    try:
        value = json.loads(base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {'store_id', 'family', 'scope', 'selector', 'h', 'last'}:
        return None
    if (value['store_id'], value['family'], value['scope'], value['selector']) != (identity, family, scope, selector):
        return None
    if type(value['h']) is not int or not 0 <= value['h'] <= maximum:
        return None
    last = value['last']
    if family == 'list':
        if not isinstance(last, list) or len(last) != 2 or type(last[0]) is not int or not isinstance(last[1], str):
            return None
        if not 0 <= last[0] <= value['h']:
            return None
    elif type(last) is not int or not 0 <= last <= value['h']:
        return None
    return value


def _token(identity, family, scope, selector, highwater, last):
    return _encode_token(dict(store_id=identity, family=family, scope=scope,
                              selector=selector, h=highwater, last=last))


def _item(row):
    value = dict(row)
    value['state'] = value.pop('observed_state')
    value['revision'] = value.pop('observed_item_revision')
    value['fields'] = None if value['fields'] is None else json.loads(value['fields'])
    return value


_SNAPSHOT = ('WITH latest AS (SELECT item_id,MAX(position) AS p FROM queue_events '
             'WHERE position<=? GROUP BY item_id) '
             'SELECT i.*,e.state AS observed_state,e.revision AS observed_item_revision,'
             'e.position AS events_position FROM queue_items i '
             'JOIN latest l ON l.item_id=i.item_id JOIN queue_events e ON e.position=l.p ')


def list_items(conn, scope: str, state_filter, kind_filter, page_size: int, continuation: str | None):
    _page_size(page_size)
    try:
        states = _filters(state_filter, kinds.STATES)
        item_kinds = _filters(kind_filter, kinds.KINDS)
    except (TypeError, ValueError):
        return _refusal('subject_unresolved', code='E_QUEUE_INVALID')
    selector = request_digest('queue.list', dict(state_filter=states, kind_filter=item_kinds))
    identity = records.store_id(conn)
    maximum = records.next_position(conn) - 1
    highwater, last = maximum, None
    if continuation is not None:
        value = _decode_token(continuation, identity, 'list', scope, selector, maximum)
        if value is None:
            return _refusal('stale_continuation', code='E_QUEUE_INVALID')
        highwater, last = value['h'], value['last']
    clauses = ["(?='machine' OR i.project_id=?)", 'i.raised_position<=?']
    params = [highwater, scope, scope, highwater]
    for column, selected in (('e.state', states), ('i.kind', item_kinds)):
        if selected is not None:
            clauses.append(column + ' IN (' + ','.join('?' for _ in selected) + ')' if selected else '0')
            params.extend(selected)
    if last is not None:
        clauses.append('(i.raised_position,i.item_id)>(?,?)')
        params.extend(last)
    params.append(page_size + 1)
    rows = _dicts(conn, _SNAPSHOT + 'WHERE ' + ' AND '.join(clauses)
                  + ' ORDER BY i.raised_position,i.item_id LIMIT ?', params)
    selected = rows[:page_size]
    token = None
    if len(rows) > page_size:
        tail = selected[-1]
        token = _token(identity, 'list', scope, selector, highwater,
                       [tail['raised_position'], tail['item_id']])
    return dict(items=[_item(row) for row in selected], store_id=identity,
                observed_revision=highwater, queue_event_position=highwater, continuation=token)


def get_item(conn, item_id, history_page_size: int, history_continuation: str | None):
    _page_size(history_page_size)
    identity = records.store_id(conn)
    maximum = records.next_position(conn) - 1
    selector = request_digest('queue.get', dict(item_id=item_id))
    highwater, last = maximum, 0
    if history_continuation is not None:
        value = _decode_token(history_continuation, identity, 'history', item_id, selector, maximum)
        if value is None:
            return _refusal('stale_continuation', code='E_QUEUE_INVALID')
        highwater, last = value['h'], value['last']
    rows = _dicts(conn, _SNAPSHOT + 'WHERE i.item_id=? AND i.raised_position<=?',
                  (highwater, item_id, highwater))
    if not rows:
        return _refusal('item_not_found')
    item = _item(rows[0])
    answers = _dicts(conn, 'SELECT answer_id,answer,answered_at_ms,phase,last_reason,outcome '
                     'FROM queue_answers WHERE item_id=?', (item_id,))
    item['answer'] = None
    item['job'] = None
    item['outcome'] = None
    if answers:
        answer = answers[0]
        item['answer'] = {name: answer[name] for name in ('answer_id', 'answer', 'answered_at_ms')}
        item['job'] = {name: answer[name] for name in ('phase', 'last_reason')}
        item['outcome'] = answer['outcome']
    events = _dicts(conn, 'SELECT * FROM queue_events WHERE item_id=? AND position>? AND position<=? '
                   'ORDER BY position LIMIT ?', (item_id, last, highwater, history_page_size + 1))
    selected = events[:history_page_size]
    token = None
    if len(events) > history_page_size:
        token = _token(identity, 'history', item_id, selector, highwater, selected[-1]['position'])
    return dict(item=item, events=selected, continuation=token, store_id=identity,
                observed_revision=highwater, queue_event_position=highwater)


def ingest_skips(catalogue_conn, *, limit: int) -> tuple[IngestSkip, ...]:
    if type(limit) is not int or limit < 1:
        raise ValueError('limit must be a positive integer')
    return tuple(IngestSkip(*row) for row in records._rows(catalogue_conn,
                 'SELECT source,row_ref,reason,skipped_at_ms FROM queue_ingest_skips '
                 'ORDER BY skipped_at_ms DESC,source,row_ref LIMIT ?', (limit,)))
