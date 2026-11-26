"""Platform composition and wire adapters for the operator queue."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from relay_engine import api_compose, api_definitions as d, api_protocol, api_wire
from relay_engine.operator_queue import kinds

if TYPE_CHECKING:
    from relay_engine.api_submit import ServiceContext
    from relay_engine.api_writer import StorePort
    from relay_engine.operator_queue import records, saga


@dataclass(frozen=True)
class _CalleeJob:
    files: frozenset[str]
    run: Callable


class PlatformJobPort:
    """Adapt Queue callbacks to the five Store port modes."""
    def __init__(self, ctx: 'ServiceContext'):
        self._store = self.over_port(ctx.port)._store

    @classmethod
    def over_port(cls, port: StorePort) -> PlatformJobPort:
        adapter = cls.__new__(cls)
        adapter._store = port
        return adapter

    @staticmethod
    def _call(submit, *args):
        from relay_engine import api_writer
        from relay_engine.operator_queue import records

        try:
            return submit(*args)
        except api_writer.WriterUnavailable as exc:
            raise records.JobUnavailable(exc.reason) from exc

    @staticmethod
    def _value(outcome):
        if outcome.indeterminate:
            from relay_engine import api_writer

            error = api_writer.TurnIndeterminate(outcome.fault)
            if isinstance(outcome.value, BaseException):
                raise error from outcome.value
            raise error
        if outcome.fault == 'stop_unwritten':
            from relay_engine.operator_queue import records

            raise records.JobUnavailable('stopped')
        return outcome.value

    def _declared(self, ledgers, fn, labels):
        from relay_engine import api_writer, store_steps
        from relay_engine.operator_queue import records

        def run(connections):
            value = fn(*(connections[label] for label in labels))
            return api_writer.JobOutcome(not isinstance(value, records.QueueRefusal), value)

        job = api_writer.DeclaredJob(store_steps.Declared('written', ledgers), run)
        return self._value(self._call(self._store.submit_declared, job))

    def catalogue(self, fn):
        return self._declared({}, fn, ('catalogue',))

    def catalogue_with_ledger_read(self, project_id, fn):
        return self._declared({project_id: 'read'}, fn, ('catalogue', project_id))

    def callee_ledger(self, project_id, fn):
        from relay_engine import api_writer

        def run(conn):
            return api_writer.JobOutcome(True, fn(conn))

        path = self._call(self._store.ledger_file, project_id)
        job = _CalleeJob(frozenset({path}), run)
        return self._value(self._call(self._store.submit_callee, project_id, job))

    def read_ledger(self, project_id, fn):
        return self._call(self._store.read, project_id, fn)

    def read_catalogue(self, fn):
        return self._call(self._store.read_catalogue, fn)


class _CatalogueOnly:
    def __init__(self, job_port: PlatformJobPort):
        self._port = job_port

    def catalogue(self, fn):
        return self._port.catalogue(fn)

    def read_catalogue(self, fn):
        return self._port.read_catalogue(fn)

    def _unavailable(self):
        from relay_engine.operator_queue import records

        raise records.JobUnavailable('store_held')

    def catalogue_with_ledger_read(self, project_id, fn):
        self._unavailable()

    def callee_ledger(self, project_id, fn):
        self._unavailable()

    def read_ledger(self, project_id, fn):
        self._unavailable()


def catalogue_only(job_port: PlatformJobPort) -> records.JobPort:
    return _CatalogueOnly(job_port)


def _normalize_raise(args):
    from relay_engine.operator_queue import records

    kind, subkind = args['kind'], args['subkind']
    key = kinds.key_value(kind, subkind, args['condition_key'])
    if isinstance(key, records.QueueRefusal):
        return key
    pins = kinds.pins_value(kind, subkind, args['pins'])
    if isinstance(pins, records.QueueRefusal):
        return pins
    return dict(args, condition_key=key, pins=pins)


_INTEGER_ARGS = {
    'queue.answer': 'expected_revision',
    'queue.dismiss': 'expected_revision',
    'queue.dismiss-all': 'up_to_event_position',
    'queue.get': 'history_page_size',
    'queue.list': 'page_size',
    'queue.open': 'expected_revision',
    'queue.withdraw': 'expected_revision',
}


def args_from_wire(operation: str, args: Mapping) -> Mapping | records.QueueRefusal:
    if operation == 'queue.raise':
        return _normalize_raise(args)
    member = _INTEGER_ARGS[operation]
    return dict(args, **{member: int(args[member])})


def _wire_value(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if isinstance(value, Mapping):
        return {key: _wire_value(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_value(member) for member in value]
    return value


def result_to_wire(operation: str, result: Mapping) -> Mapping:
    wire = _wire_value(result)
    if operation == 'queue.raise' and 'replayed' in result:
        wire['receipt'] = 'replayed' if result['replayed'] else 'original'
        del wire['replayed']
    return wire


def _read_snapshot(conn, read):
    if conn.in_transaction:
        return read(conn)
    conn.execute('BEGIN').close()
    try:
        return read(conn)
    finally:
        conn.rollback()


def _restore_fields(conn, items):
    from relay_engine.operator_queue import records

    # In-process operations decode fields. The wire carries the stored text.
    targets = [item for item in items if item['fields'] is not None]
    if not targets:
        return
    placeholders = ','.join('?' for _ in targets)
    stored = dict(records._rows(conn, 'SELECT item_id,fields FROM queue_items WHERE item_id IN ('
                               + placeholders + ')', [item['item_id'] for item in targets]))
    for item in targets:
        item['fields'] = stored[item['item_id']]


def _list(conn, args):
    from relay_engine.operator_queue import operations, records

    result = operations.list_items(conn, args['scope'], args['state_filter'],
        args['kind_filter'], args['page_size'], args['continuation'])
    if not isinstance(result, records.QueueRefusal):
        _restore_fields(conn, result['items'])
    return result


def _get(conn, args):
    from relay_engine.operator_queue import operations, records

    result = operations.get_item(conn, args['item_id'], args['history_page_size'],
                                 args['history_continuation'])
    if not isinstance(result, records.QueueRefusal):
        _restore_fields(conn, (result['item'],))
    return result


def _question_top_seat(ledger, project_id, item):
    from relay_engine import addresses
    from relay_engine.operator_queue import records

    if item['kind'] != 'question' or item['project_id'] != project_id:
        raise ValueError('question_ledger_binding_changed')
    row = records._one(ledger,
        'SELECT p.top_seat_id,t.name,s.role FROM projects p'
        ' LEFT JOIN seats s ON s.id=p.top_seat_id'
        ' LEFT JOIN team_instances t ON t.id=s.team_instance_id'
        ' AND t.project_id=p.id AND t.closed_at IS NULL WHERE p.id=?',
        (project_id,))
    if row is None:
        raise ValueError('question_project_missing')
    seat_id, team, role = row
    if seat_id is None:
        return None
    if team is None or role is None:
        raise ValueError('question_top_seat_invalid')
    return addresses.render(addresses.Address(team=team, role=role))


class QueueService:
    def __init__(self, port: records.JobPort, runtime: saga.QueueRuntime, clock,
                 enabled: frozenset[str] = kinds.ENABLED_TAGS):
        self.port = port
        self.runtime = runtime
        self.clock = clock
        self.enabled = enabled

    def run(self, operation: str, args: Mapping, principal) -> Mapping | records.QueueRefusal:
        from relay_engine.operator_queue import operations, records

        if not self.runtime.ready:
            return records.QueueRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
        if operation == 'queue.list':
            return self.port.read_catalogue(lambda conn: _read_snapshot(conn, partial(_list, args=args)))
        if operation == 'queue.get':
            return self.port.read_catalogue(lambda conn: _read_snapshot(conn, partial(_get, args=args)))
        now = self.clock()
        if operation == 'queue.raise':
            normalized = _normalize_raise(args)
            if isinstance(normalized, records.QueueRefusal):
                return normalized
            request = records.RaiseRequest(**normalized)
            result = self.port.catalogue(lambda conn: records.raise_item(
                conn, principal, request, now, enabled=self.enabled))
            if isinstance(result, records.QueueRefusal):
                return result
            return dict(item_id=result.item_id, revision=result.revision, state=result.state,
                        receipt=result.receipt_id, replayed=result.replayed)
        key = args['operation_key']
        if operation == 'queue.dismiss-all':
            return self.port.catalogue(lambda conn: operations.dismiss_all(
                conn, principal, args['scope'], args['up_to_event_position'], key, now))
        item, revision = args['item_id'], args['expected_revision']
        if operation == 'queue.withdraw':
            if not isinstance(principal, api_protocol.SeatBinding):
                return records.QueueRefusal('E_API_AUTH', 'not_asking_seat')
            return self.port.catalogue_with_ledger_read(principal.project_id,
                lambda conn, ledger: operations.withdraw(conn, ledger, principal, item,
                                                           revision, args['reason_text'], key, now))
        if operation == 'queue.answer':
            return self._answer(principal, item, revision, args['answer'], key, now)
        if operation == 'queue.dismiss':
            action = operations.dismiss
        elif operation == 'queue.open':
            action = operations.open_notice
        else:
            raise ValueError('unknown_queue_operation')
        return self.port.catalogue(lambda conn: action(conn, principal, item, revision, key, now))

    def _answer(self, principal, item_id, revision, answer, key, now):
        from relay_engine import api_writer
        from relay_engine.operator_queue import operations, records, saga

        request = saga.answer_request(item_id, revision, answer)
        inspect = partial(saga.answer_preflight, principal=principal,
                          operation_key=key, args=request, frank_conformed=False)
        checked = self.port.read_catalogue(partial(_read_snapshot, read=inspect))
        if not isinstance(checked, saga.ValidatedAnswer):
            return checked

        record = partial(saga.record_answer, principal=principal, item_id=item_id,
                         expected_revision=revision, answer=request['answer'],
                         operation_key=key, now_ms=now, frank_conformed=False,
                         top_seat_address=None)
        if checked.item['kind'] != 'question':
            return self.port.catalogue(record)

        project_id = checked.item['project_id']
        entered = False
        def record_in_project(conn, ledger):
            nonlocal entered
            entered = True
            return record(conn, resolve_top_seat=partial(_question_top_seat, ledger, project_id))

        try:
            return self.port.catalogue_with_ledger_read(project_id, record_in_project)
        except records.JobUnavailable as error:
            if entered or not isinstance(error.__cause__, api_writer.WriterUnavailable):
                raise
            # Store declined before the callback. Another turn may have recorded
            # the receipt since preflight, even though this ledger is unavailable.
            replay = None
            try:
                replay = self.port.read_catalogue(lambda conn: operations._replay(
                    conn, principal, 'queue.answer', key, request))
            except records.JobUnavailable:
                pass
            if replay is None:
                raise
            return replay

    def clear_condition(self, principal, item_id, kind, subkind, project_id, condition_key):
        from relay_engine.operator_queue import records

        if not self.runtime.ready:
            return records.QueueRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
        key = kinds.key_value(kind, subkind, condition_key)
        if isinstance(key, records.QueueRefusal):
            return key
        now = self.clock()
        return self.port.catalogue(lambda conn: records.clear_condition(
            conn, principal, item_id, kind, subkind, project_id, key, now))

    def open_condition_items(self, principal, project_id):
        from relay_engine.operator_queue import records

        if not self.runtime.ready:
            return records.QueueRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
        return self.port.read_catalogue(lambda conn: records.open_condition_items(
            conn, principal, project_id))


def queue_port_factory(ctx: ServiceContext) -> QueueService:
    from relay_engine.operator_queue import saga

    return QueueService(PlatformJobPort(ctx), saga.QueueRuntime(ctx.submit_context), ctx.clock)


_WITHDRAW_REFUSALS = {
    ('E_API_AUTH', 'session_unknown'): ('E_API_AUTH', 'session_not_current'),
    ('E_API_UNAVAILABLE', 'store_unavailable'): ('E_API_UNAVAILABLE', 'runtime_not_ready'),
}


def _handle(operation, args, principal, port):
    from relay_engine.operator_queue import records

    decoded = args_from_wire(operation, args)
    try:
        result = decoded if isinstance(decoded, records.QueueRefusal) else port.run(
            operation, decoded, principal)
    except records.JobUnavailable:
        return api_protocol.ComponentRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
    if isinstance(result, records.QueueRefusal):
        pair = (result.code, result.cause_kind)
        if operation == 'queue.withdraw':
            pair = _WITHDRAW_REFUSALS.get(pair, pair)
        payload = result_to_wire(operation, result.payload) if result.payload is not None else None
        return api_protocol.ComponentRefusal(*pair, payload)
    return result_to_wire(operation, result)


_OPERATIONS = (
    ('queue.answer', 'QueueAnswerArgs', 'QueueAnswerResult', 'operator'),
    ('queue.dismiss', 'QueueItemActionArgs', 'QueueItemActionResult', 'operator'),
    ('queue.dismiss-all', 'QueueDismissAllArgs', 'QueueDismissAllResult', 'operator'),
    ('queue.get', 'QueueGetArgs', 'QueueGetResult', 'unsigned'),
    ('queue.list', 'QueueListArgs', 'QueueListResult', 'unsigned'),
    ('queue.open', 'QueueItemActionArgs', 'QueueItemActionResult', 'operator'),
    ('queue.raise', 'QueueRaiseArgs', 'QueueRaiseResult', 'service'),
    ('queue.withdraw', 'QueueWithdrawArgs', 'QueueItemActionResult', 'seat_session'),
)

_SEAL_CAUSES = ('folder_unusable', 'counter_unusable', 'high_water_unusable', 'pointer_unset',
                'ledger_chain_mismatch', 'pointer_damaged')

_KEY_FIELDS = {
    'cross_project_permission': (('relay_id', 'OpaqueId'), ('body_sha256', 'Digest'),
                                 ('receiver_project_id', 'OpaqueId')),
    'merge_go_ahead': (('claim_id', 'OpaqueId'), ('head', 'OpaqueId')),
    'verification_held': (('claim_relay_id', 'OpaqueId'), ('request_relay_id', 'OpaqueId')),
    'doorbell_off': (('seat_id', 'OpaqueId'), ('generation', 'QueueInteger')),
    'approval_seal_unusable': (('chain_id', 'OpaqueId'), ('cause', 'QueueSealCause')),
    'approval_seal_backup_included': (('value', 'QueueBackupExclusion'),),
    'notifier_unhealthy': (('value', 'QueueNotifyWorker'),),
    'worker_stalled': (('value', 'QueueStalledWorker'),),
    'backup_missed': (('value', 'QueueDaily'),),
    'bridge_refused': (('value', 'OpaqueId'),),
    'panel_key_enrolled': (('value', 'OpaqueId'),),
    'seat_record_missing': (('seat_id', 'OpaqueId'), ('session_digest', 'Digest')),
    'seat_ambiguous': (('host', 'QueueSeatHost'), ('session_digest', 'Digest')),
}

_PINS_FIELDS = {
    'cross_project_permission': (('relay_id', 'OpaqueId'), ('expected_body_sha256', 'Digest'),
                                 ('expected_allow_revision', 'QueueInteger')),
    'merge_go_ahead': (('claim_id', 'OpaqueId'), ('repository_id', 'OpaqueId'),
                      ('pr', 'QueueInteger'), ('head', 'OpaqueId'), ('target_branch', 'OpaqueId')),
    'verification_held': (('claim_relay_id', 'OpaqueId'), ('claim_kind', 'QueueClaimKind'),
                          ('expected_request_relay_id', 'QueueNullableId')),
    'doorbell_off': _KEY_FIELDS['doorbell_off'],
    'approval_seal_unusable': _KEY_FIELDS['approval_seal_unusable'],
    'approval_seal_backup_included': (('folder', 'QueueSealFolder'),),
    'seat_record_missing': (('seat_id', 'OpaqueId'),),
}


def queue_definitions() -> d.DefinitionGraph:
    ref = d.TypeRef
    types = []

    def add(name, shape):
        types.append(d.TypeDef(name, shape))

    def record(name, fields, *, optional=()):
        add(name, d.Record(tuple(d.FieldDef(key, ref(value), key in optional)
                                 for key, value in fields)))

    def literal(name, values):
        add(name, d.LiteralSet(tuple(sorted(values))))

    def tagged(name, prefix, arms):
        refs = []
        for tag, fields in arms.items():
            arm = prefix + ''.join(part.title() for part in tag.split('_'))
            literal(arm + 'Kind', (tag,))
            record(arm, (('kind', arm + 'Kind'), *fields))
            refs.append(ref(arm))
        add(name, d.TaggedUnion('kind', tuple(refs)))

    for name, values in (
        ('QueueKind', kinds.KINDS), ('QueueSubkind', kinds.NOTICE_SUBKINDS | {'approval_seal_unusable', 'approval_seal_backup_included'}),
        ('QueueEventKind', kinds.EVENT_KINDS), ('QueueFlag', ('true', 'false')),
        ('QueueRaiseReceipt', ('original', 'replayed')),
        ('QueueSealCause', _SEAL_CAUSES), ('QueueBackupExclusion', ('backup_exclusion',)),
        ('QueueNotifyWorker', ('notify_worker',)), ('QueueStalledWorker', kinds.STALL_WORKERS),
        ('QueueDaily', ('daily',)), ('QueueSeatHost', ('claude-code', 'codex')),
        ('QueueClaimKind', ('design', 'plan')), ('QueueSealFolder', ('approval_seal',)),
        ('QueueJobPhase', ('recorded', 'confirmed', 'faulted')),
        ('QueuePrincipalKind', ('internal', 'operator', 'seat', 'service')),
    ):
        literal(name, values)

    add('ItemId', d.String(r'[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
                           36, 36))
    add('QueueInteger', d.String(kinds.INTEGER_PATTERN, 1, 15))
    literal('QueueState', kinds.STATES)
    add('QueuePageSize', d.String(r'(?:[1-9][0-9]{0,2}|1000)', 1, 4))
    add('QueueDisplayText', d.String(r'[^\x00]+', 1, 4096))
    add('QueueText', d.String(r'[^\x00]*', 0, api_wire.MAX_API_FRAME))
    for name, inner in (
        ('QueueNullableInteger', 'QueueInteger'), ('QueueNullableId', 'OpaqueId'),
        ('QueueNullableText', 'QueueText'), ('QueueNullableSubkind', 'QueueSubkind'),
        ('QueueNullableContinuation', 'QueueText'), ('QueueNullableAnswer', 'QueueAnswerView'),
        ('QueueNullableJob', 'QueueJobView'), ('QueueNullableStates', 'QueueStates'),
        ('QueueNullableKinds', 'QueueKinds'),
    ):
        add(name, d.Nullable(ref(inner)))
    for name, inner, limit in (
        ('QueueStates', 'QueueState', len(kinds.STATES)), ('QueueKinds', 'QueueKind', len(kinds.KINDS)),
        ('QueueItems', 'QueueItem', 1000), ('QueueEvents', 'QueueEvent', 1000),
        ('QueueItemIds', 'ItemId', 1000),
    ):
        add(name, d.Array(ref(inner), limit))

    tagged('QueueConditionKey', 'QueueKey', _KEY_FIELDS)
    tagged('QueuePins', 'QueuePins', {tag: _PINS_FIELDS.get(tag, ()) for tag in _KEY_FIELDS})
    record('QueueTexts', tuple((name, 'QueueDisplayText')
                               for name in ('sentence', 'subject', 'caused_by', 'ring_text')),
           optional=('subject', 'caused_by', 'ring_text'))
    record('QueueAnswer', (('choice', 'QueueText'), ('text', 'QueueText')),
           optional=('choice', 'text'))

    action = (('item_id', 'ItemId'), ('expected_revision', 'QueueInteger'),
              ('operation_key', 'OpaqueId'))
    record('QueueItemActionArgs', action)
    record('QueueAnswerArgs', (*action, ('answer', 'QueueAnswer')))
    record('QueueWithdrawArgs', (*action, ('reason_text', 'QueueNullableText')))
    record('QueueDismissAllArgs', (('scope', 'OpaqueId'), ('up_to_event_position', 'QueueInteger'),
                                   ('operation_key', 'OpaqueId')))
    record('QueueGetArgs', (('item_id', 'ItemId'), ('history_page_size', 'QueuePageSize'),
                            ('history_continuation', 'QueueNullableContinuation')))
    record('QueueListArgs', (('scope', 'OpaqueId'), ('state_filter', 'QueueNullableStates'),
        ('kind_filter', 'QueueNullableKinds'), ('page_size', 'QueuePageSize'),
        ('continuation', 'QueueNullableContinuation')))
    record('QueueRaiseArgs', (('kind', 'QueueKind'), ('subkind', 'QueueNullableSubkind'),
        ('project_id', 'QueueNullableId'), ('texts', 'QueueTexts'), ('pins', 'QueuePins'),
        ('condition_key', 'QueueConditionKey'), ('actor_address', 'QueueNullableText'),
        ('operation_key', 'OpaqueId')))

    receipt = (('receipt', 'OpaqueId'), ('replayed', 'QueueFlag'))
    item_state = (('item_id', 'ItemId'), ('revision', 'QueueInteger'), ('state', 'QueueState'))
    record('QueueRaiseResult', (*item_state, ('receipt', 'QueueRaiseReceipt')))
    record('QueueItemActionResult', (*item_state, *receipt))
    record('QueueAnswerResult', (*item_state, ('answer_id', 'OpaqueId'), *receipt))
    record('QueueDismissAllResult', (('count', 'QueueInteger'), ('item_ids', 'QueueItemIds'),
                                    ('more_remaining', 'QueueFlag'), *receipt))
    item = (*item_state, ('kind', 'QueueKind'), ('subkind', 'QueueNullableSubkind'),
        ('project_id', 'QueueNullableId'), ('raiser', 'OpaqueId'),
        ('asking_seat_id', 'QueueNullableId'), ('actor_address', 'QueueNullableText'),
        ('fields', 'QueueNullableText'), ('answer_to', 'QueueNullableText'),
        ('source_handle', 'QueueNullableInteger'), ('source_relay_id', 'QueueNullableId'),
        ('texts', 'QueueText'), ('popup_text', 'QueueNullableText'),
        ('choices', 'QueueNullableText'), ('choices_reason', 'QueueNullableText'),
        ('pins', 'QueueText'), ('condition_key', 'QueueText'), ('raised_position', 'QueueInteger'),
        ('raised_at_ms', 'QueueInteger'), ('events_position', 'QueueInteger'))
    record('QueueItem', item)
    record('QueueAnswerView', (('answer_id', 'OpaqueId'), ('answer', 'QueueText'),
                               ('answered_at_ms', 'QueueInteger')))
    record('QueueJobView', (('phase', 'QueueJobPhase'), ('last_reason', 'QueueNullableText')))
    record('QueueDetailItem', (*item, ('answer', 'QueueNullableAnswer'),
        ('job', 'QueueNullableJob'), ('outcome', 'QueueNullableText')))
    record('QueueEvent', (*item_state, ('position', 'QueueInteger'), ('event_kind', 'QueueEventKind'),
        ('principal_kind', 'QueuePrincipalKind'), ('principal_id', 'OpaqueId'),
        ('receipt_id', 'QueueNullableId'), ('detail', 'QueueNullableText'),
        ('at_ms', 'QueueInteger')))
    read = (('store_id', 'OpaqueId'), ('observed_revision', 'QueueInteger'),
            ('queue_event_position', 'QueueInteger'), ('continuation', 'QueueNullableContinuation'))
    record('QueueListResult', (('items', 'QueueItems'), *read))
    record('QueueGetResult', (('item', 'QueueDetailItem'), ('events', 'QueueEvents'), *read))

    error = ref('ApiError')
    definitions = tuple(d.OperationDef(
        name, ref(request), ref(result), error, principal,
        'pure_observation' if principal == 'unsigned' else 'catalogue_write',
        'read_repeat' if principal == 'unsigned' else 'operation_key',
        name if principal == 'unsigned' else 'none',
    ) for name, request, result, principal in _OPERATIONS)
    return d.DefinitionGraph(tuple(types), definitions, error)


_QUEUE_HANDLERS = {name: partial(_handle, name) for name, *_ in _OPERATIONS}


def queue_component() -> api_compose.ApiComponent:
    return api_compose.ApiComponent('console', queue_definitions(), _QUEUE_HANDLERS,
                                   queue_port_factory)
