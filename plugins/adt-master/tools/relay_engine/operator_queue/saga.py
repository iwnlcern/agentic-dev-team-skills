"""Durable answer recording; owning-ledger apply is a separate step."""
from collections.abc import Mapping
import json

from relay_engine import header
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.operator_queue import kinds, operations, records
from relay_engine.submit import new_uuid7


def answer_draft(item: records.ItemView, choice: str | None, text: str,
                 top_seat_address: str | None) -> bytes:
    texts = json.loads(item.texts)
    subject = ('Operator answer: ' + texts['subject'])[:400]
    lines = ['KIND: SITREP', 'TO: ' + item.answer_to]
    if top_seat_address is not None and top_seat_address != item.answer_to:
        lines.append('CC: ' + top_seat_address)
    lines.extend(['PARENT: ' + item.source_relay_id, 'NEEDS: none', 'SUBJECT: ' + subject])
    body = 'Item: ' + item.item_id + '\n'
    if choice is not None:
        body += 'Choice: ' + choice + '\n'
    body += 'Text: ' + text
    encoded = body.encode('utf-8')
    if len(encoded) > header.BODY_CAP:
        raise ValueError('text_too_long')
    return ('\n'.join(lines) + '\n\n').encode('utf-8') + encoded


def record_answer(conn, principal: OperatorPrincipal, item_id, expected_revision,
                  answer: Mapping, operation_key, now_ms, *, frank_conformed: bool,
                  top_seat_address: str | None):
    if not isinstance(principal, OperatorPrincipal):
        return records.QueueRefusal('E_API_AUTH', 'not_operator')
    args = dict(item_id=item_id, expected_revision=expected_revision, answer=dict(answer))
    replay = operations._replay(conn, principal, 'queue.answer', operation_key, args)
    if replay is not None:
        return replay
    rows = operations._dicts(conn, 'SELECT * FROM queue_items WHERE item_id=?', (item_id,))
    if not rows:
        return records.QueueRefusal('E_QUEUE_STATE', 'item_not_found')
    item = rows[0]
    if item['revision'] != expected_revision:
        return records.QueueRefusal('E_QUEUE_STATE', 'revision_mismatch',
            {'current_revision': item['revision'], 'current_state': item['state']})
    if item['state'] != 'pending':
        return records.QueueRefusal('E_QUEUE_STATE', 'item_not_pending')
    if not records.route_active(conn, item['project_id']):
        return records.QueueRefusal('E_QUEUE_STATE', 'project_not_active')
    choice, text = answer.get('choice'), answer.get('text', '')
    if principal.surface not in kinds.answer_surfaces(item['kind'], choice, frank_conformed=frank_conformed):
        return records.QueueRefusal('E_QUEUE_INVALID', 'surface_not_allowed')
    fixed = kinds.fixed_choices(item['kind'])
    if fixed and choice not in fixed:
        return records.QueueRefusal('E_QUEUE_INVALID', 'choice_not_allowed')
    draft = None
    if item['kind'] == 'question':
        if item['answer_to'] is None:
            return records.QueueRefusal('E_QUEUE_STATE', 'answer_unroutable')
        position = records._one(conn, 'SELECT MAX(position) FROM queue_events WHERE item_id=?', (item_id,))[0]
        view = records.ItemView(**item, events_position=position)
        try:
            draft = answer_draft(view, choice, text, top_seat_address)
        except ValueError as exc:
            if str(exc) != 'text_too_long':
                raise
            return records.QueueRefusal('E_QUEUE_INVALID', 'text_too_long')
    answer_id = new_uuid7(now_ms)
    apply_key = 'adt-queue-v1:' + records.store_id(conn) + ':' + item_id + ':' + answer_id
    revision = item['revision'] + 1
    records._write(conn, 'INSERT INTO queue_answers '
        '(item_id,answer_id,answer,answered_at_ms,apply_key,draft,project_id,phase) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (item_id, answer_id, records._json(answer), now_ms, apply_key, draft, item['project_id'], 'recorded'))
    records._write(conn, "UPDATE queue_items SET state='applying',revision=? WHERE item_id=?", (revision, item_id))
    result = dict(item_id=item_id, revision=revision, state='applying', answer_id=answer_id)
    completed = operations._complete(conn, principal, 'queue.answer', operation_key, args, result, now_ms)
    records.append_event(conn, item_id, revision, 'applying', 'answered', principal, now_ms,
                         receipt_id=completed['receipt'])
    return completed
