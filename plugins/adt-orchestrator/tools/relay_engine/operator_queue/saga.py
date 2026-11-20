"""Durable answer recording; owning-ledger apply is a separate step."""
from collections.abc import Mapping
import json
import sqlite3
from dataclasses import asdict, dataclass

from relay_engine import errors, header, queries, submit
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.operator_queue import kinds, operations, records
from relay_engine.submit import new_uuid7


def answer_draft(item: records.ItemView, choice: str | None, text: str,
                 top_seat_address: str | None) -> bytes:
    texts = json.loads(item.texts)
    subject = ('Operator answer: ' + texts['subject'])[:400].rstrip()
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


def answer_request(item_id, expected_revision, answer):
    return dict(item_id=item_id, expected_revision=expected_revision, answer=dict(answer))


@dataclass(frozen=True)
class ValidatedAnswer:
    item: Mapping


def answer_preflight(conn, principal, operation_key, args, *, frank_conformed):
    if not isinstance(principal, OperatorPrincipal):
        return records.QueueRefusal('E_API_AUTH', 'not_operator')
    replay = operations._replay(conn, principal, 'queue.answer', operation_key, args)
    if replay is not None:
        return replay
    rows = operations._dicts(conn, 'SELECT * FROM queue_items WHERE item_id=?', (args['item_id'],))
    if not rows:
        return records.QueueRefusal('E_QUEUE_STATE', 'item_not_found')
    item = rows[0]
    if item['revision'] != args['expected_revision']:
        return records.QueueRefusal('E_QUEUE_STATE', 'revision_mismatch',
            {'current_revision': item['revision'], 'current_state': item['state']})
    if item['state'] != 'pending':
        return records.QueueRefusal('E_QUEUE_STATE', 'item_not_pending')
    if item['kind'] == 'notice':
        return records.QueueRefusal('E_QUEUE_INVALID', 'surface_not_allowed')
    if not records.route_active(conn, item['project_id']):
        return records.QueueRefusal('E_QUEUE_STATE', 'project_not_active')
    choice = args['answer'].get('choice')
    if principal.surface not in kinds.answer_surfaces(item['kind'], choice, frank_conformed=frank_conformed):
        return records.QueueRefusal('E_QUEUE_INVALID', 'surface_not_allowed')
    fixed = kinds.fixed_choices(item['kind'])
    if fixed and choice not in fixed:
        return records.QueueRefusal('E_QUEUE_INVALID', 'choice_not_allowed')
    if item['kind'] == 'question' and item['answer_to'] is None:
        return records.QueueRefusal('E_QUEUE_STATE', 'answer_unroutable')
    return ValidatedAnswer(item)


def record_answer(conn, principal: OperatorPrincipal, item_id, expected_revision,
                  answer: Mapping, operation_key, now_ms, *, frank_conformed: bool,
                  top_seat_address: str | None, resolve_top_seat=None):
    args = answer_request(item_id, expected_revision, answer)
    checked = answer_preflight(conn, principal, operation_key, args,
                               frank_conformed=frank_conformed)
    if not isinstance(checked, ValidatedAnswer):
        return checked
    item = checked.item
    if resolve_top_seat is not None:
        top_seat_address = resolve_top_seat(item)
    draft = None
    if item['kind'] == 'question':
        position = records._one(conn, 'SELECT MAX(position) FROM queue_events WHERE item_id=?', (item_id,))[0]
        view = records.ItemView(**item, events_position=position)
        choice, text = args['answer'].get('choice'), args['answer'].get('text', '')
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




@dataclass(frozen=True)
class ApplyJob:
    item_id: str
    answer_id: str
    project_id: str
    apply_key: str
    draft: bytes | None
    call_args: Mapping | None
    source_relay_id: str | None
    source_handle: int | None
    answer_to: str | None


@dataclass(frozen=True)
class ApplyResult:
    status: str
    receipt: str | None
    stale_pin: str | None
    code: str | None
    reason: str | None


class QueueRuntime:
    def __init__(self, make_context=None):
        self.ready = False
        self.make_context = make_context


@dataclass(frozen=True)
class ReconcileReport:
    visited: int
    confirmed: int
    transient: int


def load_apply_job(catalogue_conn, item_id) -> ApplyJob:
    row = records._one(catalogue_conn,
        'SELECT a.item_id,a.answer_id,a.project_id,a.apply_key,a.draft,a.call_args,'
        'i.source_relay_id,i.source_handle,i.answer_to FROM queue_answers a '
        'JOIN queue_items i ON i.item_id=a.item_id WHERE a.item_id=?', (item_id,))
    if row is None:
        raise KeyError(item_id)
    return ApplyJob(*row[:5], None if row[5] is None else json.loads(row[5]), *row[6:])


def apply_question(ledger_conn, job: ApplyJob, make_context) -> ApplyResult:
    assert not ledger_conn.in_transaction, 'apply_question requires a callee-owned connection'
    context = make_context(ledger_conn, job.project_id,
        submit.SeatRef('operator', 'operator', 'operator', None, job.project_id), job.apply_key)
    try:
        admitted = queries.operator_admission(ledger_conn, job.apply_key, job.draft)
        if admitted is not None:
            return ApplyResult('applied', admitted.relay_id, None, None, None)
        rows = queries.operator_questions(ledger_conn, job.project_id, job.source_handle - 1, 1)
        if (not rows or rows[0].relay_id != job.source_relay_id
                or rows[0].sender_address != job.answer_to):
            return ApplyResult('refused', None, None, 'E_QUEUE_STATE answer_unroutable', None)
        admitted = submit.submit(ledger_conn, context, job.draft)
        return ApplyResult('applied', admitted.relay_id, None, None, None)
    except errors.EngineError as exc:
        if exc.code == 'E-STORE-BUSY':
            return ApplyResult('transient', None, None, None, 'E-STORE-BUSY')
        status = 'faulted' if exc.code == 'E-SUBMISSION-KEY-REUSED' else 'refused'
        return ApplyResult(status, None, None, exc.code, None)
    except sqlite3.OperationalError as exc:
        if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
            raise
        return ApplyResult('transient', None, None, None, str(exc))


def confirm(catalogue_conn, item_id, result: ApplyResult, now_ms) -> None:
    conn = catalogue_conn
    answer = records._one(conn, 'SELECT phase,apply_key FROM queue_answers WHERE item_id=?', (item_id,))
    if answer is None or answer[0] != 'recorded':
        return
    if result.status == 'transient':
        records._write(conn, 'UPDATE queue_answers SET last_reason=? WHERE item_id=?', (result.reason, item_id))
        return
    states = {'applied': 'resolved', 'stale': 'superseded', 'refused': 'refused', 'faulted': 'faulted'}
    state = states[result.status]
    item = operations._dicts(conn, 'SELECT * FROM queue_items WHERE item_id=?', (item_id,))[0]
    revision = item['revision'] + 1
    principal = records.InternalWriter()
    records._write(conn, 'UPDATE queue_items SET state=?,revision=? WHERE item_id=?', (state, revision, item_id))
    records._write(conn, 'UPDATE queue_answers SET phase=?,last_reason=?,outcome=? WHERE item_id=?',
        ('faulted' if result.status == 'faulted' else 'confirmed',
         result.code if result.status == 'faulted' else None, records._json(asdict(result)), item_id))
    detail = ({'stale_pin': result.stale_pin} if result.status == 'stale' else
              {'code': result.code} if result.status in ('refused', 'faulted') else {'receipt': result.receipt})
    event = 'applied' if result.status == 'applied' else state
    records.append_event(conn, item_id, revision, state, event, principal, now_ms, detail=detail)
    condition = records._one(conn, 'SELECT clear_requested,closed_at_ms FROM queue_conditions WHERE item_id=?', (item_id,))
    if condition is not None and condition[0] and condition[1] is None:
        records._close_condition(conn, item_id, revision, state, principal, now_ms)
    if result.status == 'faulted':
        request = records.RaiseRequest('notice', 'answer_faulted', item['project_id'],
            {'sentence': kinds.ANSWER_FAULTED_TEXTS[item['kind']]}, {},
            {'project_id': item['project_id'], 'item_id': item_id}, answer[1],
            actor_address=item['actor_address'])
        notice = records.raise_item(conn, principal, request, now_ms, enabled=kinds.ENABLED_TAGS)
        if isinstance(notice, records.QueueRefusal):
            raise RuntimeError('stopped-answer notice refused: ' + notice.cause_kind)
    if result.status != 'faulted' and kinds.rings(item['kind'], item['subkind']):
        records._write(conn, 'INSERT INTO queue_notifications '
            '(item_id,revision,state,next_attempt_at_ms) VALUES (?,?,?,?)',
            (item_id, revision, 'pending', now_ms))


def reconcile_pass(port: records.JobPort, runtime: QueueRuntime, now_ms,
                   *, stopping=lambda: False) -> ReconcileReport:
    items = port.read_catalogue(lambda conn: records._rows(conn,
        "SELECT item_id FROM queue_answers WHERE phase='recorded' ORDER BY item_id"))
    visited = confirmed = transient = 0
    for (item_id,) in items:
        if stopping():
            break
        visited += 1
        job = port.read_catalogue(lambda conn: load_apply_job(conn, item_id))
        try:
            result = port.callee_ledger(job.project_id,
                lambda conn: apply_question(conn, job, runtime.make_context))
        except records.JobUnavailable as exc:
            result = ApplyResult('transient', None, None, None, exc.reason)
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
            result = ApplyResult('transient', None, None, None, str(exc))
        port.catalogue(lambda conn: confirm(conn, item_id, result, now_ms()))
        if result.status == 'transient':
            transient += 1
        else:
            confirmed += 1
    runtime.ready = visited == len(items)
    return ReconcileReport(visited, confirmed, transient)
