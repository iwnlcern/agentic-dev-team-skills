"""Registered Queue behavior and Store transaction adaptation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from relay_engine import api_compose, api_platform, api_protocol, api_schema, api_submit, api_wire
from relay_engine import api_writer, store_layout, store_steps
from relay_engine.api_principals import OperatorPrincipal, ServicePrincipal
from relay_engine.operator_queue import ingest, kinds, operations, records, saga
from relay_engine.tests.test_catalogue import P, Q
from relay_engine.tests import test_queue_ingest, test_queue_records
from relay_engine.tests.writer_support import serve_for_test


QUEUE_OPS = (
    'queue.answer', 'queue.dismiss', 'queue.dismiss-all', 'queue.get',
    'queue.list', 'queue.open', 'queue.raise', 'queue.withdraw',
)
PAGE = OperatorPrincipal('page')
ENGINE = ServicePrincipal('engine_liveness')
AUTHORITY = ServicePrincipal('authority_service')
LIST_ARGS = dict(scope='machine', state_filter=None, kind_filter=None, page_size='100',
                 continuation=None)


def doorbell(key='raise-1', generation='7'):
    subject = dict(kind='doorbell_off', seat_id='s-a', generation=generation)
    return dict(kind='notice', subkind='doorbell_off', project_id=P,
                texts={'sentence': 'Doorbell unavailable'}, pins=dict(subject),
                condition_key=subject, actor_address=None, operation_key=key)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        from relay_engine.operator_queue import component

        self.component = component
        self.fixture = test_queue_records.TestQueueRecords()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.port = test_queue_records.FileJobPort(self.fixture.root)
        self.runtime = saga.QueueRuntime()
        self.runtime.ready = True
        self.service = component.QueueService(
            self.port, self.runtime, lambda: 1000, enabled=test_queue_records.ALL)
        self.build = api_platform.build_platform()
        self.registry = self.build.registry
        self.ports = dict.fromkeys(QUEUE_OPS, self.service)
        artifact = api_schema.export_schema(self.registry, '3.0')
        self.api_runtime = api_protocol.RuntimeContext('store', 'isolated', 'queue', '3.0.0',
            'a' * 64, True, {'3.0': artifact})
        self.connected = api_protocol.dispatch_api(
            api_protocol.initial_connection_state(),
            api_wire.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
            self.api_runtime, self.registry, self.ports)
        self.state = self.connected.state

    def dispatch(self, name, args, principal=None, binding=None):
        state = replace(self.state, binding=binding)
        return api_protocol.dispatch_api(state, api_wire.ApiRequest('2', name, args),
            self.api_runtime, self.registry, self.ports, principal=principal)

    def call(self, name, args, principal=None, binding=None):
        return self.dispatch(name, args, principal, binding).response

    def raise_notice(self, **changes):
        args = dict(doorbell(), **changes)
        return self.call('queue.raise', args, ENGINE)['result']

    def test_component_composes(self):
        composition = self.build.composition
        expected = {
            'queue.raise': ('service', 'catalogue_write', 'operation_key', 'none'),
            'queue.withdraw': ('seat_session', 'catalogue_write', 'operation_key', 'none'),
            'queue.answer': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.dismiss': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.open': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.dismiss-all': ('operator', 'catalogue_write', 'operation_key', 'none'),
            'queue.get': ('unsigned', 'pure_observation', 'read_repeat', 'queue.get'),
            'queue.list': ('unsigned', 'pure_observation', 'read_repeat', 'queue.list'),
        }
        rows = {op.name: (op.principal, op.effect, op.retry, op.browser_get)
                for op in self.registry.operations.values() if op.name.startswith('queue.')}
        self.assertEqual(rows, expected)
        self.assertEqual({name: owner for name, owner in composition.owners.items()
                          if name.startswith('queue.')}, dict.fromkeys(QUEUE_OPS, 'console'))
        self.assertEqual({name for name in self.registry.handlers if name.startswith('queue.')},
                         set(QUEUE_OPS))
        self.assertIs(composition.port_factories['console'], self.component.queue_port_factory)
        queue = self.component.queue_component()
        platform = api_platform.platform_component()
        with self.assertRaises(api_compose.CompositionError):
            api_compose.compose_components((platform, queue, queue))
        shared = platform.definitions.types[0]
        altered = replace(queue, definitions=replace(queue.definitions,
            types=queue.definitions.types + (shared,)))
        with self.assertRaises(api_compose.CompositionError):
            api_compose.compose_components((platform, altered))

    def test_component_import_opens_nothing(self):
        path = str(Path(__file__).resolve().parents[2])
        program = """
import builtins, socket, sqlite3, sys
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("unexpected I/O")
builtins.open = socket.socket = sqlite3.connect = forbidden
from relay_engine.operator_queue import component
try:
    component.queue_port_factory(None)
except (AttributeError, TypeError, AssertionError):
    pass
else:
    raise AssertionError("factory accepted an invalid context")
"""
        subprocess.run([sys.executable, '-I', '-B', '-c', program, path], check=True,
                       stdin=subprocess.DEVNULL, timeout=20)

    def test_package_import_boundary_and_exports(self):
        path = str(Path(__file__).resolve().parents[2])
        program = """
import sys
sys.path.insert(0, sys.argv[1])
order = sys.argv[2]
if order == "package-first":
    import relay_engine.operator_queue as package
    from relay_engine.operator_queue import component
elif order == "component-first":
    import relay_engine.operator_queue.component
    import relay_engine.operator_queue as package
else:
    import relay_engine.operator_queue.records
    import relay_engine.operator_queue as package

exports = {"undo_step", "UndoQueueResult", "UndoBlocked", "BlockingItem"}
forbidden = {"relay_engine.operator_queue.records", "sqlite3", "relay_engine.api_writer"}
if order != "records-first":
    assert not forbidden.intersection(sys.modules), "eager Queue runtime import"
    assert exports <= set(dir(package)), "missing export before access"
    assert not forbidden.intersection(sys.modules), "introspection loaded runtime"

try:
    package.unknown_queue_member
except AttributeError:
    pass
else:
    raise AssertionError("unknown package member accepted")

from relay_engine.operator_queue import undo_step, UndoQueueResult, UndoBlocked, BlockingItem
from relay_engine.operator_queue import kinds, records
assert kinds is sys.modules["relay_engine.operator_queue.kinds"]
assert records is sys.modules["relay_engine.operator_queue.records"]
assert undo_step is records.undo_step
assert UndoQueueResult is records.UndoQueueResult
assert UndoBlocked is records.UndoBlocked
assert BlockingItem is records.BlockingItem
namespace = {}
exec("from relay_engine.operator_queue import *", namespace)
assert set(namespace) - {"__builtins__"} == exports
for name in exports:
    assert namespace[name] is vars(records)[name]
"""
        for order in ('package-first', 'component-first', 'records-first'):
            with self.subTest(order=order):
                result = subprocess.run([sys.executable, '-I', '-B', '-c', program, path, order],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_not_ready_refuses(self):
        notice = self.raise_notice()
        action = dict(item_id=notice['item_id'], expected_revision='1', operation_key='action')
        requests = (
            ('queue.raise', doorbell(), ENGINE, None),
            ('queue.withdraw', dict(action, reason_text=None), None, self.fixture.seat),
            ('queue.answer', dict(action, answer={'text': 'Answer'}), PAGE, None),
            ('queue.dismiss', action, PAGE, None), ('queue.open', action, PAGE, None),
            ('queue.dismiss-all', dict(scope='machine', up_to_event_position='1',
                                      operation_key='all'), PAGE, None),
            ('queue.list', LIST_ARGS, None, None),
            ('queue.get', dict(item_id=notice['item_id'], history_page_size='10',
                              history_continuation=None), None, None),
        )
        before = self.fixture.snapshot()
        self.runtime.ready = False
        for name, args, principal, binding in requests:
            with self.subTest(name=name):
                reply = self.call(name, args, principal, binding)
                self.assertEqual(reply['error']['cause'], {'kind': 'runtime_not_ready'})
        again = api_protocol.dispatch_api(api_protocol.initial_connection_state(),
            api_wire.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
            self.api_runtime, self.registry, self.ports)
        self.assertEqual(again.response, self.connected.response)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_operator_and_service_gates(self):
        notice = self.raise_notice()
        action = dict(item_id=notice['item_id'], expected_revision='1', operation_key='action')
        requests = {
            'queue.answer': dict(action, answer={'text': 'Answer'}),
            'queue.dismiss': action, 'queue.open': action,
            'queue.dismiss-all': dict(scope='machine', up_to_event_position='1', operation_key='all'),
        }
        before = self.fixture.snapshot()
        with patch.object(self.service, 'run', wraps=self.service.run) as handler:
            for name, args in requests.items():
                for binding in (None, self.fixture.seat):
                    self.assertEqual(self.call(name, args, binding=binding)['error']['code'],
                                     'E_API_AUTH')
            self.assertEqual(self.call('queue.raise', doorbell(),
                                      binding=self.fixture.seat)['error']['code'], 'E_API_AUTH')
            handler.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.call('queue.open', action, PAGE)['result']['revision'], '2')

    def test_page_size_bounds(self):
        with patch.object(self.service, 'run', wraps=self.service.run) as handler:
            for size in ('0', '1001', '1000000000000000'):
                error = self.call('queue.list', dict(LIST_ARGS, page_size=size))['error']
                self.assertEqual(error['cause'], {'kind': 'invalid_args'})
            handler.assert_not_called()
            self.assertEqual(self.call('queue.list', dict(LIST_ARGS, page_size='1000'))
                             ['result']['items'], [])
            handler.assert_called_once()

    def test_registered_reads_unsigned_and_stored_text(self):
        notice = self.raise_notice()
        args = dict(item_id=notice['item_id'], history_page_size='100', history_continuation=None)
        before = self.fixture.snapshot()
        with patch.object(self.service, 'run', wraps=self.service.run) as handler:
            for binding in (None, self.fixture.seat):
                listed = self.call('queue.list', LIST_ARGS, binding=binding)['result']
                result = self.call('queue.get', args, binding=binding)['result']
                item = result['item']
                self.assertEqual(item['condition_key'], '{"generation":7,"seat_id":"s-a"}')
                self.assertEqual(item['pins'], '{"generation":7,"seat_id":"s-a"}')
                self.assertEqual((item['revision'], item['raised_at_ms']), ('1', '1000'))
                self.assertIsNone(result['events'][0]['detail'])
                self.assertEqual(listed['items'][0]['item_id'], notice['item_id'])
                self.assertEqual((listed['observed_revision'], listed['queue_event_position']),
                                 ('1', '1'))
            self.assertEqual([call.args[2] for call in handler.call_args_list], [None] * 4)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_in_process_raise_takes_either_form(self):
        tagged = doorbell()
        first = self.service.run('queue.raise', tagged, ENGINE)
        stored = dict(generation=7, seat_id='s-a')
        verified = dict(tagged, pins=stored, condition_key=stored)
        second = self.service.run('queue.raise', verified, ENGINE)
        self.assertEqual(second['item_id'], first['item_id'])
        self.assertEqual(second['receipt'], first['receipt'])
        self.assertTrue(second['replayed'])
        revised = dict(verified, condition_key=dict(stored, generation=8))
        self.assertEqual(self.service.run('queue.raise', revised, ENGINE).cause_kind,
                         'operation_key_reused')
        separate = self.service.run('queue.raise', dict(verified, operation_key='another'), ENGINE)
        self.assertEqual(separate['item_id'], first['item_id'])
        self.assertEqual(len(self.service.open_condition_items(ENGINE, P)), 1)

    def test_service_clear_and_recover(self):
        for argument_kind in ('tagged', 'returned'):
            args = doorbell(argument_kind)
            item = self.call('queue.raise', args, ENGINE)['result']
            self.assertEqual(self.service.open_condition_items(AUTHORITY, P), [])
            handle, = self.service.open_condition_items(ENGINE, P)
            self.assertEqual(handle.condition_key, dict(generation=7, seat_id='s-a'))
            key = args['condition_key'] if argument_kind == 'tagged' else handle.condition_key
            common = (handle.item_id, handle.kind, handle.subkind, P)
            self.assertEqual(self.service.clear_condition(AUTHORITY, *common, key).cause_kind,
                             'subject_unresolved')
            self.assertEqual(self.service.clear_condition(ENGINE, *common, key).outcome, 'cleared')
            durable = self.fixture.snapshot()
            self.assertEqual(self.service.clear_condition(ENGINE, *common, key).outcome,
                             'already_closed')
            self.assertEqual(self.fixture.snapshot(), durable)
        self.runtime.ready = False
        with patch.object(self.port, 'catalogue') as write, patch.object(self.port, 'read_catalogue') as read:
            self.assertEqual(self.service.clear_condition(ENGINE, *common, key).cause_kind,
                             'runtime_not_ready')
            self.assertEqual(self.service.open_condition_items(ENGINE, P).cause_kind,
                             'runtime_not_ready')
            write.assert_not_called()
            read.assert_not_called()

    def test_integer_arguments_bounded(self):
        args = dict(scope='machine', up_to_event_position='999999999999999', operation_key='all')
        self.assertEqual(self.call('queue.dismiss-all', args, PAGE)['result']['count'], '0')
        before = self.fixture.snapshot()
        for value in ('1000000000000000', '9007199254740992', '18446744073709551615'):
            error = self.call('queue.dismiss-all', dict(args, up_to_event_position=value), PAGE)
            self.assertEqual(error['error']['cause']['kind'], 'invalid_args')
        self.assertEqual(self.fixture.snapshot(), before)
        notice = self.raise_notice()
        for value in (10**15, 2**53, '1000000000000000'):
            key = dict(seat_id='s-a', generation=value)
            if type(value) is str:
                key['kind'] = 'doorbell_off'
            outcome = self.service.clear_condition(
                ENGINE, notice['item_id'], 'notice', 'doorbell_off', P, key)
            self.assertEqual(outcome.cause_kind, 'subject_unresolved')

    def test_raise_key_and_pin_shapes_and_texts(self):
        valid = doorbell()
        before = self.fixture.snapshot()
        for name in ('condition_key', 'pins'):
            subject = valid[name]
            for bad in (dict(subject, extra='bad'), dict(subject, generation=7),
                        dict(subject, kind='unknown'), dict(subject, generation='1000000000000000'),
                        {'kind': 'doorbell_off', 'seat_id': 's-a'}):
                reply = self.call('queue.raise', dict(valid, **{name: bad}), ENGINE)
                self.assertEqual(reply['error']['cause']['kind'], 'invalid_args')
        for texts in ({'unexpected': 'text'}, {'sentence': ''}, {'sentence': 'a' * 4097},
                      {'sentence': 'a\0b'}):
            reply = self.call('queue.raise', dict(valid, texts=texts), ENGINE)
            self.assertEqual(reply['error']['cause']['kind'], 'invalid_args')
        for changes in (
            dict(texts={'sentence': 'é' * 2048 + 'a'}),
            dict(condition_key={'kind': 'notifier_unhealthy', 'value': 'notify_worker'}),
        ):
            reply = self.call('queue.raise', dict(valid, **changes), ENGINE)
            self.assertEqual(reply['error']['cause']['kind'], 'subject_unresolved')
        self.assertEqual(self.fixture.snapshot(), before)
        maximum = doorbell(generation='999999999999999')
        maximum['texts'] = {'sentence': 'é' * 2048}
        first = self.call('queue.raise', maximum, ENGINE)['result']
        item = self.call('queue.get', dict(item_id=first['item_id'],
            history_page_size='100', history_continuation=None))['result']['item']
        self.assertEqual(json.loads(item['pins'])['generation'], 999999999999999)

    def test_registered_bulk_limit_and_preexisting_bound(self):
        for number in range(1500):
            self.fixture.raise_it(request=self.fixture.request(key=str(number)))
        question = self.fixture.raise_it(self.fixture.internal,
            self.fixture.request('question', None, key='question'))
        bound = records.next_position(self.fixture.conn) - 1
        later = self.fixture.raise_it(request=self.fixture.request(key='later'))
        self.fixture.conn.commit()

        args = dict(scope='machine', up_to_event_position=str(bound), operation_key='first')
        first = self.call('queue.dismiss-all', args, PAGE)['result']
        second = self.call('queue.dismiss-all', dict(args, operation_key='second'), PAGE)['result']
        self.assertEqual((first['count'], first['more_remaining']), ('1000', 'true'))
        self.assertEqual((second['count'], second['more_remaining']), ('500', 'false'))
        self.assertEqual(len(set(first['item_ids']) | set(second['item_ids'])), 1500)
        for item_id in (question.item_id, later.item_id):
            item = self.call('queue.get', dict(item_id=item_id, history_page_size='1',
                history_continuation=None))['result']['item']
            self.assertEqual(item['state'], 'pending')

    def test_raise_seal_pins_and_dormant_wire_arms(self):
        seal = ServicePrincipal('approval_seal')
        arms = {
            'approval_seal_unusable': (dict(chain_id='c', cause='pointer_unset'),
                                       dict(chain_id='c', cause='pointer_unset')),
            'approval_seal_backup_included': (dict(value='backup_exclusion'),
                                              dict(folder='approval_seal')),
            'seat_record_missing': (dict(seat_id='s-a', session_digest='a' * 64),
                                    dict(seat_id='s-a')),
            'seat_ambiguous': (dict(host='codex', session_digest='a' * 64), {}),
        }
        before = self.fixture.snapshot()
        for tag, (key, pins) in arms.items():
            args = dict(doorbell(), subkind=tag, project_id=None,
                        operation_key=tag, condition_key=dict(key, kind=tag),
                        pins=dict(pins, kind=tag))
            self.assertEqual(self.call('queue.raise', args, seal)['error']['cause']['kind'],
                             'kind_not_raisable')
            for member in ('condition_key', 'pins'):
                bad = dict(args[member], extra='value')
                self.assertEqual(self.call('queue.raise', dict(args, **{member: bad}), seal)
                                 ['error']['cause']['kind'], 'invalid_args')
        self.assertEqual(self.fixture.snapshot(), before)

        # Task 10 exposes the wire arms while the pinned production catalogue
        # still rejects seal rows. Enable persistence only in this test fixture.
        def with_seal_rows(fn):
            def apply(conn):
                conn.execute('PRAGMA ignore_check_constraints=ON').close()
                try:
                    return fn(conn)
                finally:
                    conn.execute('PRAGMA ignore_check_constraints=OFF').close()
            return self.port.run(self.fixture.root / 'machine.sqlite3', apply, transaction=True)

        for tag, project_id in (('approval_seal_unusable', P),
                                ('approval_seal_backup_included', None)):
            key, pins = arms[tag]
            row = kinds.RaiserRow('notice', tag, 'queue.raise', 'approval_seal', 'pr5', None)
            with (patch.object(kinds, 'RAISER_MATRIX', (*kinds.RAISER_MATRIX, row)),
                  patch.object(self.port, 'catalogue', side_effect=with_seal_rows)):
                args = dict(doorbell(), subkind=tag, project_id=project_id, operation_key=tag,
                            condition_key=dict(key, kind=tag), pins=dict(pins, kind=tag))
                reply = self.call('queue.raise', args, seal)
                self.assertIn('result', reply, f'{tag}: {reply!r}')
                raised = reply['result']
                retried = self.service.run('queue.raise', dict(args, pins=pins,
                    condition_key=key.get('value', key)), seal)
                self.assertEqual(retried['item_id'], raised['item_id'])
                self.assertTrue(retried['replayed'])
                item = self.call('queue.get', dict(item_id=raised['item_id'],
                    history_page_size='1', history_continuation=None))['result']['item']
                self.assertEqual(json.loads(item['pins']), pins)

    def test_registered_worker_checks_actor_address(self):
        subject = dict(kind='worker_stalled', value='queue_reconcile')
        args = dict(doorbell(), subkind='worker_stalled', project_id=None,
                    condition_key=subject, pins={'kind': 'worker_stalled'})
        supervisor = ServicePrincipal('supervision')
        before = self.fixture.snapshot()
        reply = self.call('queue.raise', dict(args, actor_address='operator'), supervisor)
        self.assertEqual(reply['error']['cause']['kind'], 'subject_unresolved')
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.call('queue.raise', args, supervisor)['result']['revision'], '1')

    def test_get_preserves_fields_event_detail_and_integer_boundary(self):
        fields = '{"act_key":[7,3]}'
        item = self.fixture.raise_it(self.fixture.internal,
            self.fixture.request('notice', 'operator_act_carried'),
            fields={'act_key': [7, 3]})
        conn = self.fixture.conn
        detail = {'before': 1, 'after': 2}
        maximum = 999999999999999
        records.append_event(conn, item.item_id, maximum, 'pending', 'opened', PAGE, 301,
                             detail=detail)
        conn.execute('UPDATE queue_items SET revision=? WHERE item_id=?',
                     (maximum, item.item_id)).close()
        conn.commit()
        args = dict(item_id=item.item_id, history_page_size='100', history_continuation=None)
        before = self.fixture.snapshot()
        reply = self.call('queue.get', args)['result']
        self.assertEqual(reply['item']['fields'], fields)
        self.assertEqual(reply['item']['revision'], str(maximum))
        self.assertEqual({event['detail'] for event in reply['events']},
                         {None, '{"after":2,"before":1}'})
        self.assertEqual(self.fixture.snapshot(), before)

        conn.execute('UPDATE queue_items SET revision=? WHERE item_id=?',
                     (maximum + 1, item.item_id)).close()
        records.append_event(conn, item.item_id, maximum + 1, 'pending', 'opened', PAGE, 302)
        conn.commit()
        before = self.fixture.snapshot()
        self.assertEqual(self.call('queue.get', args)['error']['cause']['kind'], 'handler_fault')
        self.assertEqual(self.fixture.snapshot(), before)

    def test_public_bulk_dismiss_replays_legacy_receipt(self):
        self.raise_notice()
        self.fixture.conn.commit()
        original = self.port.catalogue(lambda conn: operations.dismiss_all(
            conn, PAGE, 'machine', 1, 'legacy', 1000))
        before = self.fixture.snapshot()
        args = dict(scope='machine', up_to_event_position='1', operation_key='legacy')
        replay = self.call('queue.dismiss-all', args, PAGE)['result']
        self.assertEqual(replay['receipt'], original['receipt'])
        self.assertEqual(replay['item_ids'], original['item_ids'])
        self.assertEqual(replay['count'], '1')
        self.assertEqual(replay['more_remaining'], 'false')
        self.assertEqual(replay['replayed'], 'true')
        self.assertEqual(self.call('queue.dismiss-all', dict(args, up_to_event_position='2'),
                                   PAGE)['error']['cause']['kind'], 'operation_key_reused')
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertIsNone(self.registry.operation('queue.dismiss_all'))


class WriterPortTests(unittest.TestCase):
    def setUp(self):
        from relay_engine.operator_queue import component

        self.component = component
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name) / 'store'
        self.project = P
        with closing(store_layout.open_catalogue(root, kind='isolated')) as catalogue:
            catalogue.execute(
                "INSERT INTO project_routes (project_id,name,state,created_at)"
                " VALUES (?,?,'active',0)", (self.project, 'original')).close()
            catalogue.commit()
        with closing(store_layout.open_ledger(root, self.project, create=True)) as ledger:
            ledger.execute('INSERT INTO projects (id,name,created_at) VALUES (?,?,0)',
                           (self.project, 'original')).close()
            ledger.commit()
        self.writer = api_writer.StoreWriter(root, runtime_instance_id='queue-port')
        self.writer.start()
        self.addCleanup(self.writer.close)
        serve_for_test(self.writer, self.project)
        self.context = api_submit.ServiceContext(self.writer, lambda: 1000, str(root), self.project)
        self.port = component.PlatformJobPort(self.context)

    def test_declared_job_vote_and_value(self):
        original = self.writer.submit_declared
        seen = []
        def spy(job):
            def run(connections):
                self.assertTrue(all(conn.in_transaction for conn in connections.values()))
                outcome = job.run(connections)
                seen.append((job.declared, outcome))
                return outcome
            return original(replace(job, run=run))

        with patch.object(self.writer, 'submit_declared', side_effect=spy) as calls:
            self.assertEqual(self.port.catalogue(lambda c: records.store_id(c)),
                             self.writer.store_id())
            def mutate(catalogue, ledger):
                project_name, = records._one(ledger, 'SELECT name FROM projects WHERE id=?',
                                             (self.project,))
                records._write(catalogue, 'UPDATE project_routes SET name=? WHERE project_id=?',
                               ('updated', self.project))
                return project_name

            self.assertEqual(self.port.catalogue_with_ledger_read(self.project, mutate), 'original')
            self.assertEqual(calls.call_count, 2)
        self.assertEqual(seen, [
            (store_steps.Declared('written', {}), api_writer.JobOutcome(True, self.writer.store_id())),
            (store_steps.Declared('written', {self.project: 'read'}),
             api_writer.JobOutcome(True, 'original')),
        ])
        self.assertEqual(self.port.read_catalogue(lambda c: records._one(
            c, 'SELECT name FROM project_routes WHERE project_id=?', (self.project,))), ('updated',))
        self.assertEqual(self.port.read_ledger(self.project, lambda c: records._one(
            c, 'SELECT name FROM projects WHERE id=?', (self.project,))), ('original',))

    def test_declared_refusal_preserves_value_on_rollback(self):
        refusal = records.QueueRefusal('E_QUEUE_STATE', 'item_not_found')
        def reject(conn):
            records._write(conn, 'UPDATE project_routes SET name=? WHERE project_id=?',
                           ('rollback witness', self.project))
            return refusal

        self.assertIs(self.port.catalogue(reject), refusal)
        self.assertEqual(self.port.read_catalogue(lambda c: records._one(
            c, 'SELECT name FROM project_routes WHERE project_id=?', (self.project,))), ('original',))

    def test_declared_outcome_indeterminate_is_not_success(self):
        for fault in ('job_ended_transaction', 'rollback_failed'):
            with self.subTest(fault=fault):
                bogus = api_writer.JobOutcome(False, {'receipt': 'bogus'}, True, fault)
                with patch.object(self.writer, 'submit_declared', return_value=bogus) as submit:
                    with self.assertRaisesRegex(api_writer.TurnIndeterminate, fault):
                        self.port.catalogue(lambda c: None)
                    submit.assert_called_once()

    def test_callbacks_keep_the_writer_outcome(self):
        from relay_engine.operator_queue import ingest

        log = Path(self.context.store_root).parent / 'lifecycle.jsonl'
        log.write_bytes(b'')
        for outcome, adapter_error, callback_error in (
            (api_writer.JobOutcome(False, None, True, 'x'),
             api_writer.TurnIndeterminate, api_writer.TurnIndeterminate),
            (api_writer.JobOutcome(False, None, False, 'stop_unwritten'),
             records.JobUnavailable, ingest.StartupIngestIncomplete),
        ):
            read_catalogue = self.writer.read_catalogue
            port = SimpleNamespace(submit_declared=lambda job: outcome,
                                   read_catalogue=read_catalogue)
            ctx = replace(self.context, port=port)
            adapters = (self.component.PlatformJobPort.over_port(port),
                        self.component.PlatformJobPort(ctx))
            for adapter in adapters:
                with self.assertRaises(adapter_error) as caught:
                    adapter.catalogue(lambda c: self.fail('scripted outcome executed the job'))
                if outcome.indeterminate:
                    self.assertEqual(caught.exception.args, ('x',))
                else:
                    self.assertEqual(caught.exception.reason, 'stopped')
            for callback, source in ((ingest.startup_callback(log), 'lifecycle_log'),
                                     (ingest.project_start_callback(), 'stop_events')):
                with self.assertRaises(callback_error) as caught:
                    callback(port)
                if outcome.indeterminate:
                    self.assertEqual(caught.exception.args, ('x',))
                else:
                    self.assertEqual((caught.exception.source, caught.exception.cause),
                                     (source, 'job_unavailable'))

    def test_post_commit_cleanup_fault_preserves_receipt(self):
        original = self.writer.submit_declared
        def with_cleanup_fault(job):
            outcome = original(job)
            self.assertTrue(outcome.commit)
            return replace(outcome, fault='rollback_failed')
        def write(catalogue, ledger):
            return operations.dismiss_all(catalogue, PAGE, self.project, 0, 'committed', 1000)

        with patch.object(self.writer, 'submit_declared', side_effect=with_cleanup_fault) as submit:
            value = self.port.catalogue_with_ledger_read(self.project, write)
            submit.assert_called_once()
        self.assertEqual(self.port.read_catalogue(lambda c: records._one(
            c, 'SELECT receipt_id FROM queue_receipts')), (value['receipt'],))

    def test_writer_unavailable_is_job_unavailable(self):
        for reason in ('busy', 'closed', 'store_held', 'rollback_failed', 'catalogue_faulted',
                       'project_fenced', 'project_not_served'):
            refusal = api_writer.WriterUnavailable(reason)
            unavailable = SimpleNamespace(**dict.fromkeys(
                ('submit_declared', 'submit_callee', 'read', 'read_catalogue'),
                lambda *a: raise_error(refusal)), ledger_file=self.writer.ledger_file)
            port = self.component.PlatformJobPort(replace(self.context, port=unavailable))
            for invoke in (port.catalogue, port.read_catalogue,
                           lambda cb: port.callee_ledger(self.project, cb),
                           lambda cb: port.read_ledger(self.project, cb),
                           lambda cb: port.catalogue_with_ledger_read(self.project, cb)):
                with self.assertRaises(records.JobUnavailable) as caught:
                    invoke(lambda *c: None)
                self.assertEqual(caught.exception.reason, reason)
        expected = ValueError('callback failed')
        def fail(catalogue):
            records._write(catalogue, 'UPDATE project_routes SET name=? WHERE project_id=?',
                           ('rollback witness', self.project))
            raise expected
        with self.assertRaises(ValueError) as caught:
            self.port.catalogue(fail)
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.port.read_catalogue(lambda c: records._one(
            c, 'SELECT name FROM project_routes WHERE project_id=?', (self.project,))), ('original',))


def raise_error(error):
    raise error


class RegisteredWriterTests(unittest.TestCase):
    def setUp(self):
        from relay_engine import api_sessions
        from relay_engine.operator_queue import component

        self.f = test_queue_ingest.QueueFixture(self)
        self.question = self.f.admit()
        root = self.f.root / 'store'
        self.ledger_path = store_layout.ledger_path(root, P)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.ledger_path)) as ledger:
            self.f.ledger.backup(ledger)
            # QueueFixture has two projects; the served Store file holds only P.
            ledger.execute('DELETE FROM projects WHERE id=?', (Q,)).close()
            ledger.commit()
            self.binding = api_sessions.current_binding(ledger, self.f.binding.session_id)
            self.other = api_sessions.current_binding(ledger, self.f.other_binding.session_id)
        self.writer = api_writer.StoreWriter(root, runtime_instance_id='queue')
        self.writer.start()
        self.addCleanup(self.writer.close)
        serve_for_test(self.writer, P)
        # Establish WAL side files through a served write before the first reader.
        def publish_wal(conn):
            conn.execute("UPDATE projects SET name='queue proof' WHERE id=?", (P,)).close()
            return api_writer.JobOutcome(True, None)

        self.writer.submit(P, SimpleNamespace(run=publish_wal,
            files=frozenset({str(self.ledger_path)})))
        self.context = api_submit.ServiceContext(self.writer, lambda: self.f.clock, str(root), Q)
        self.contexts = []
        build = api_platform.build_platform()
        self.registry = build.registry
        real_context = api_submit.ServiceContext.submit_context
        def context_spy(ctx, *a, **kw):
            context = real_context(ctx, *a, **kw)
            self.contexts.append((ctx, context))
            return context

        with patch.object(api_submit.ServiceContext, 'submit_context', context_spy):
            owner_ports = {owner: factory(self.context)
                           for owner, factory in build.composition.port_factories.items()}
        self.ports = {op: owner_ports[owner] for op, owner in build.composition.owners.items()}
        self.service = owner_ports['console']
        self.assertIsInstance(self.service.port, component.PlatformJobPort)
        questions = self.service.port.read_ledger(P,
            lambda c: ingest.read_questions(c, P, 0, 100))
        self.assertEqual([row.relay_id for row in questions], [self.question.relay_id])
        self.assertEqual(ingest.question_ingest_pass(self.service.port, [P], self.f.clock), 1)
        self.item, _, _, _ = self.f.items()[0]
        self.service.runtime.ready = True
        artifact = api_schema.export_schema(self.registry, '3.0')
        self.runtime = api_protocol.RuntimeContext('store', 'isolated', 'queue', '3.0.0',
            'a' * 64, True, {'3.0': artifact})
        connected = api_protocol.dispatch_api(api_protocol.initial_connection_state(),
            api_wire.ApiRequest('1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
            self.runtime, self.registry, self.ports)
        self.state = connected.state

    def dispatch(self, name, args, *, binding=None, principal=None):
        raw = json.dumps(dict(request_id='2', op=name, args=args)).encode('utf-8')
        request = api_wire.decode_api_frame(raw, self.registry)
        self.assertIsInstance(request, api_wire.ApiRequest)
        return api_protocol.dispatch_api(replace(self.state, binding=binding), request,
            self.runtime, self.registry, self.ports, principal=principal)

    def action(self, **kwargs):
        return dict(item_id=self.item, expected_revision='1', operation_key='action', **kwargs)

    def test_registered_withdraw_binding_refusals(self):
        real = operations.withdraw
        args = self.action(reason_text='Withdraw my question')
        before = self.f.snapshot()
        cases = (
            (replace(self.binding, session_id='missing'), None, 'session_unknown',
             'E_API_AUTH', 'session_not_current', 'sign_in_again', None),
            (self.binding, 'deny', 'store_unavailable',
             'E_API_UNAVAILABLE', 'runtime_not_ready', 'restore_expected_runtime', self.binding),
            (self.binding, 'replace', 'key_replaced',
             'E_API_AUTH', 'key_replaced', 'obtain_current_key', None),
            (self.binding, 'stood_down', 'seat_stood_down',
             'E_API_AUTH', 'seat_stood_down', 'ask_top_seat', None),
            (replace(self.binding, generation=29), None, 'session_not_current',
             'E_API_AUTH', 'session_not_current', 'sign_in_again', None),
            (self.other, None, 'not_asking_seat',
             'E_API_AUTH', 'not_asking_seat', 'withdraw_own_item', self.other),
        )
        for binding, fault, raw, code, cause, remedy, retained in cases:
            self.assertEqual(self.f.snapshot(), before)
            if fault in ('replace', 'stood_down'):
                with closing(sqlite3.connect(self.ledger_path)) as fixture:
                    if fault == 'replace':
                        fixture.execute('UPDATE seat_occupants SET ended_at=7 WHERE id=?',
                                        (binding.occupant_id,)).close()
                    else:
                        fixture.execute("UPDATE seats SET state='stood-down' WHERE id=?",
                                        (binding.seat_id,)).close()
                    fixture.commit()

            def withdraw(catalogue, ledger, *a):
                authorizer = None
                if fault == 'deny':
                    def authorizer(action, table, *unused):
                        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                                and table == 'seat_sessions' else sqlite3.SQLITE_OK)
                    ledger.set_authorizer(authorizer)
                try:
                    result = real(catalogue, ledger, *a)
                    self.assertEqual(result.cause_kind, raw)
                    return result
                finally:
                    if authorizer is not None:
                        ledger.set_authorizer(None)
            with self.subTest(cause=cause), patch.object(operations, 'withdraw', side_effect=withdraw):
                result = self.dispatch('queue.withdraw', args, binding=binding)
                error = result.response['error']
                self.assertEqual((error['code'], error['cause'], error['remedy']),
                                 (code, {'kind': cause}, remedy))
                self.assertFalse(result.close)
                self.assertEqual(result.state.binding, retained)
            if fault in ('replace', 'stood_down'):
                with closing(sqlite3.connect(self.ledger_path)) as fixture:
                    if fault == 'replace':
                        fixture.execute('UPDATE seat_occupants SET ended_at=NULL WHERE id=?',
                                        (binding.occupant_id,)).close()
                    else:
                        fixture.execute("UPDATE seats SET state='occupied' WHERE id=?",
                                        (binding.seat_id,)).close()
                    fixture.commit()
            self.assertEqual(self.f.snapshot(), before)

        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as called:
            result = self.dispatch('queue.withdraw', args, binding=self.binding)
            job = called.call_args.args[0]
            called.assert_called_once()
        self.assertEqual(job.declared, store_steps.Declared('written', {P: 'read'}))
        self.assertEqual((result.response['result']['state'], result.response['result']['revision']),
                         ('withdrawn', '2'))
        self.assertEqual(result.state.binding, self.binding)
        after = self.f.snapshot()
        for table in ('queue_receipts', 'queue_events'):
            self.assertEqual(len(after[table]), len(before[table]) + 1)
        self.assertEqual(self.f.items()[0][2:], ('withdrawn', 2))

    def fence_project(self):
        def reject_rollback(conn):
            def authorizer(action, arg, *unused):
                if action == sqlite3.SQLITE_TRANSACTION and arg == 'ROLLBACK':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorizer)
            return api_writer.JobOutcome(False, None)

        job = SimpleNamespace(run=reject_rollback,
                              files=frozenset({self.writer.ledger_file(P)}))
        with self.assertRaises(api_writer.WriterUnavailable) as caught:
            self.writer.submit(P, job)
        self.assertEqual(caught.exception.reason, 'rollback_failed')
        self.assertNotIn(P, self.writer.served_projects())

    def designate_top(self, seat_id):
        self.ledger_fixture('UPDATE projects SET top_seat_id=? WHERE id=?', (seat_id, P))

    def ledger_fixture(self, sql, args=()):
        with closing(sqlite3.connect(self.ledger_path)) as conn:
            conn.execute(sql, args).close()
            conn.commit()

    def catalogue_fixture(self, sql, args=()):
        self.f.catalogue.execute(sql, args).close()
        self.f.catalogue.commit()

    def answer_request(self, text='Answer exactly', **changes):
        args = dict(self.action(answer={'text': text}), **changes)
        return self.dispatch('queue.answer', args, principal=PAGE).response

    def stored_answer(self):
        return self.service.port.read_catalogue(lambda conn: saga.load_apply_job(conn, self.item))

    def assert_answer_draft(self, job, cc=None):
        headers, _ = job.draft.split(b'\n\n', 1)
        lines = headers.decode().splitlines()
        self.assertEqual([line for line in lines if line.startswith('TO: ')],
                         ['TO: ' + self.f.sender.address])
        self.assertEqual([line for line in lines if line.startswith('CC: ')],
                         [] if cc is None else ['CC: ' + cc])
        self.assertIn('PARENT: ' + self.question.relay_id, lines)
        self.assertEqual(job.answer_to, self.f.sender.address)

    def assert_admitted_answer(self, job, cc_seat=None):
        port = self.service.port
        report = saga.reconcile_pass(port, self.service.runtime, lambda: self.f.clock)
        self.assertEqual((report.visited, report.confirmed, report.transient), (1, 1, 0))
        relay, parent = port.read_ledger(P, lambda conn: records._one(conn,
            'SELECT id,parent_id FROM relays WHERE submission_key=?', (job.apply_key,)))
        self.assertEqual(parent, self.question.relay_id)
        recipients = port.read_ledger(P, lambda conn: records._rows(conn,
            'SELECT role,seat_id FROM recipients WHERE relay_id=?', (relay,)))
        expected = [('to', self.binding.seat_id)]
        if cc_seat is not None:
            expected.append(('cc', cc_seat))
        self.assertCountEqual(recipients, expected)
        self.assertEqual(self.f.items()[0][2:], ('resolved', 3))

    def assert_answer_error(self, reply, code, cause):
        self.assertNotIn('result', reply)
        self.assertEqual((reply['error']['code'], reply['error']['cause']['kind']), (code, cause))

    def test_registered_answer_top_seat_cc_and_replay(self):
        self.designate_top(self.other.seat_id)
        # A vacant seat on an open team retains its routing address.
        self.ledger_fixture("UPDATE seats SET state='vacant' WHERE id=?", (self.other.seat_id,))
        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as call:
            original = self.answer_request()['result']
            call.assert_called_once()
            self.assertEqual(call.call_args.args[0].declared,
                             store_steps.Declared('written', {P: 'read'}))
        self.assertNotEqual(self.context.project_id, P)
        job = self.stored_answer()
        self.assert_answer_draft(job, self.f.other.address)
        self.assert_admitted_answer(job, self.other.seat_id)
        self.designate_top(self.binding.seat_id)
        self.fence_project()
        durable = self.f.snapshot()
        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as call:
            self.assertEqual(self.answer_request()['result'], dict(original, replayed='true'))
            call.assert_not_called()
        self.assertEqual(self.stored_answer().draft, job.draft)
        self.assertEqual(self.f.snapshot(), durable)

    def test_registered_answer_same_top_seat_has_one_recipient(self):
        self.designate_top(self.binding.seat_id)
        self.assertEqual(self.answer_request()['result']['state'], 'applying')
        job = self.stored_answer()
        self.assert_answer_draft(job)
        self.assert_admitted_answer(job)

    def test_registered_answer_null_designation(self):
        self.designate_top(None)
        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as call:
            self.assertEqual(self.answer_request()['result']['state'], 'applying')
            self.assertEqual(call.call_args.args[0].declared,
                             store_steps.Declared('written', {P: 'read'}))
        self.assert_answer_draft(self.stored_answer())

    def test_registered_answer_top_seat_identity_refusals(self):
        before = self.f.snapshot()
        for seat in (self.f.foreign.seat_id, 'missing-seat'):
            with self.subTest(seat=seat):
                self.designate_top(seat)
                self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
                self.assertEqual(self.f.snapshot(), before)

        self.designate_top(self.other.seat_id)
        team = self.other.team_instance_id
        self.ledger_fixture('UPDATE team_instances SET closed_at=2 WHERE id=?', (team,))
        self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
        self.assertEqual(self.f.snapshot(), before)
        self.ledger_fixture('UPDATE team_instances SET closed_at=NULL WHERE id=?', (team,))

        self.ledger_fixture("UPDATE team_instances SET name='Invalid name' WHERE id=?", (team,))
        self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
        self.assertEqual(self.f.snapshot(), before)
        self.ledger_fixture('UPDATE team_instances SET name=? WHERE id=?',
                            (self.f.other.address.split('.')[0], team))

        self.ledger_fixture("UPDATE seats SET team_instance_id='missing-team' WHERE id=?",
                            (self.other.seat_id,))
        self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
        self.assertEqual(self.f.snapshot(), before)
        self.ledger_fixture('UPDATE seats SET team_instance_id=? WHERE id=?',
                            (team, self.other.seat_id))

        self.ledger_fixture('DELETE FROM projects WHERE id=?', (P,))
        self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
        self.assertEqual(self.f.snapshot(), before)

    def test_registered_answer_read_fault_and_changed_project(self):
        self.designate_top(self.other.seat_id)
        before = self.f.snapshot()
        submit_declared = self.writer.submit_declared

        def deny_lookup(job):
            run = job.run
            def denied(conns):
                ledger = conns[P]
                def auth(action, table, *_):
                    if action == sqlite3.SQLITE_READ and table == 'projects':
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                ledger.set_authorizer(auth)
                try:
                    return run(conns)
                finally:
                    ledger.set_authorizer(None)
            return submit_declared(replace(job, run=denied))

        with patch.object(self.writer, 'submit_declared', side_effect=deny_lookup), patch.object(
                self.writer, 'read_catalogue', wraps=self.writer.read_catalogue) as reads:
            self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
            reads.assert_called_once()
        self.assertEqual(self.f.snapshot(), before)

        def move_item(job):
            self.assertEqual(job.declared, store_steps.Declared('written', {P: 'read'}))
            self.catalogue_fixture('UPDATE queue_items SET project_id=? WHERE item_id=?', (Q, self.item))
            changed = self.f.snapshot()
            try:
                return submit_declared(job)
            finally:
                self.assertEqual(self.f.snapshot(), changed)

        with patch.object(self.writer, 'submit_declared', side_effect=move_item):
            self.assert_answer_error(self.answer_request(), 'E_API_INTERNAL', 'handler_fault')
        self.catalogue_fixture('UPDATE queue_items SET project_id=? WHERE item_id=?', (P, self.item))
        self.assertEqual(self.f.snapshot(), before)

    def test_registered_answer_ledger_unavailable(self):
        before = self.f.snapshot()
        self.fence_project()
        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as turn:
            self.assert_answer_error(self.answer_request(), 'E_API_UNAVAILABLE', 'runtime_not_ready')
            turn.assert_called_once()
        self.assertEqual(self.f.snapshot(), before)

        real_read = self.writer.read_catalogue
        for error, code, cause in (
            (api_writer.WriterUnavailable('catalogue_faulted'), 'E_API_UNAVAILABLE', 'runtime_not_ready'),
            (sqlite3.OperationalError('unexpected fixture read fault'), 'E_API_INTERNAL', 'handler_fault'),
        ):
            def catalogue_read(fn):
                if read.call_count == 1:
                    return real_read(fn)
                raise error
            with (self.subTest(cause=cause),
                  patch.object(self.writer, 'read_catalogue', side_effect=catalogue_read) as read):
                self.assert_answer_error(self.answer_request(), code, cause)
                self.assertEqual(read.call_count, 2)
            self.assertEqual(self.f.snapshot(), before)

    def test_registered_answer_preconditions_before_ledger(self):
        before = self.f.snapshot()
        missing = '00000000-0000-7000-8000-000000000001'
        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as turn:
            for changes, cause in (({'item_id': missing}, 'item_not_found'),
                                   ({'expected_revision': '2'}, 'revision_mismatch')):
                self.assert_answer_error(self.answer_request(**changes), 'E_QUEUE_STATE', cause)
            self.catalogue_fixture("UPDATE project_routes SET state='creating' WHERE project_id=?", (P,))
            self.assert_answer_error(self.answer_request(), 'E_QUEUE_STATE', 'project_not_active')
            self.catalogue_fixture("UPDATE project_routes SET state='active' WHERE project_id=?", (P,))
            self.catalogue_fixture('UPDATE queue_items SET answer_to=NULL WHERE item_id=?', (self.item,))
            self.assert_answer_error(self.answer_request(), 'E_QUEUE_STATE', 'answer_unroutable')
            self.catalogue_fixture('UPDATE queue_items SET answer_to=? WHERE item_id=?',
                                   (self.f.sender.address, self.item))
            turn.assert_not_called()
        self.assertEqual(self.f.snapshot(), before)

        submit_declared = self.writer.submit_declared
        def advance_revision(job):
            self.catalogue_fixture('UPDATE queue_items SET revision=2 WHERE item_id=?', (self.item,))
            return submit_declared(job)
        with patch.object(self.writer, 'submit_declared', side_effect=advance_revision):
            result = self.answer_request()
        self.assert_answer_error(result, 'E_QUEUE_STATE', 'revision_mismatch')
        self.assertEqual(result['error']['cause']['current_revision'], '2')
        self.catalogue_fixture('UPDATE queue_items SET revision=1 WHERE item_id=?', (self.item,))
        self.assertEqual(self.f.snapshot(), before)

    def test_non_question_answer_records_without_a_ledger(self):
        request = records.RaiseRequest(
            kind='cross_project_permission', subkind=None, project_id=P,
            texts={'sentence': 'Allow transfer'}, pins={},
            condition_key={'relay_id': 'relay', 'body_sha256': 'a' * 64,
                           'receiver_project_id': Q}, operation_key='raise-permission')
        item = self.service.port.catalogue(lambda conn: records.raise_item(
            conn, ServicePrincipal('xproject_transfer'), request, self.f.clock,
            enabled=frozenset({'pr2'})))
        self.assertIsInstance(item, records.RaiseResult)
        self.item = item.item_id
        self.fence_project()
        with patch.object(self.writer, 'submit_declared', wraps=self.writer.submit_declared) as turn:
            reply = self.dispatch('queue.answer', self.action(answer={'choice': 'deny'}),
                                  principal=PAGE).response
            self.assertEqual(reply['result']['state'], 'applying')
            turn.assert_called_once()
            self.assertEqual(turn.call_args.args[0].declared, store_steps.Declared('written', {}))
        self.assertIsNone(self.stored_answer().draft)

    def answer_decline_interleaving(self, first_text):
        self.designate_top(self.other.seat_id)
        real_submit = self.writer.submit_declared
        waiting, resume = Event(), Event()
        entered, declines = [], []
        before = self.f.snapshot()

        def pause_before_admission(job):
            self.assertEqual(job.declared, store_steps.Declared('written', {P: 'read'}))
            if waiting.is_set():
                return real_submit(job)
            waiting.set()
            if not resume.wait(timeout=10):
                raise AssertionError('fixture did not release the competing answer')
            run = job.run
            def observe_callback(conns):
                entered.append(True)
                return run(conns)
            try:
                return real_submit(replace(job, run=observe_callback))
            except api_writer.WriterUnavailable as error:
                declines.append(error.reason)
                raise

        with (ThreadPoolExecutor(max_workers=1) as worker,
              patch.object(self.writer, 'submit_declared', side_effect=pause_before_admission)):
            pending = worker.submit(self.answer_request)
            try:
                self.assertTrue(waiting.wait(timeout=10))
                original = self.answer_request(first_text)['result']
                draft = self.stored_answer().draft
                durable = self.f.snapshot()
                self.fence_project()
            finally:
                resume.set()
            competing = pending.result(timeout=10)
        self.assertFalse(entered)
        self.assertEqual(len(declines), 1)
        self.assertIn(declines[0], ('project_not_served', 'rollback_failed'))
        self.assertEqual(self.f.snapshot(), durable)
        for table in ('queue_answers', 'queue_receipts', 'queue_events'):
            self.assertEqual(len(durable[table]), len(before[table]) + 1)
        job = self.stored_answer()
        self.assertEqual(job.draft, draft)
        self.assert_answer_draft(job, self.f.other.address)
        return original, competing

    def test_registered_answer_rechecks_replay_after_declined_ledger_turn(self):
        original, competing = self.answer_decline_interleaving('Answer exactly')
        self.assertEqual(competing['result'], dict(original, replayed='true'))

    def test_registered_answer_rechecks_conflict_after_declined_ledger_turn(self):
        _, competing = self.answer_decline_interleaving('Different meaning, same key')
        self.assert_answer_error(competing, 'E_API_CONFLICT', 'operation_key_reused')

    def test_registered_answer_end_to_end(self):
        from relay_engine import slips
        answer = self.dispatch('queue.answer', self.action(answer={'text': 'Answer exactly'}),
                               principal=PAGE).response['result']
        self.assertEqual((answer['state'], answer['revision']), ('applying', '2'))
        self.assertIs(self.ports['queue.answer'], self.service)
        report = saga.reconcile_pass(self.service.port, self.service.runtime, lambda: self.f.clock)
        self.assertEqual((report.visited, report.confirmed, report.transient), (1, 1, 0))
        ctx, captured = self.contexts[0]
        self.assertIs(ctx, self.context)
        self.assertIs(captured.after_admit, slips.on_admitted)
        self.assertEqual(self.f.items()[0][2:], ('resolved', 3))
        job = self.service.port.read_catalogue(lambda c: saga.load_apply_job(c, self.item))
        relay = self.service.port.read_ledger(P, lambda c: records._one(
            c, 'SELECT parent_id FROM relays WHERE submission_key=?', (job.apply_key,)))
        self.assertEqual(relay, (self.question.relay_id,))

    def test_registered_step2_requires_callee_owned_connection(self):
        answer = self.dispatch('queue.answer', self.action(answer={'text': 'Answer exactly'}),
                               principal=PAGE)
        self.assertEqual(answer.response['result']['state'], 'applying')
        port = self.service.port
        job = port.read_catalogue(lambda c: saga.load_apply_job(c, self.item))
        with patch.object(self.writer, 'submit_callee', side_effect=self.writer.submit):
            with self.assertRaisesRegex(AssertionError, 'callee-owned connection'):
                saga.reconcile_pass(port, self.service.runtime, lambda: self.f.clock)
        self.assertEqual(self.contexts, [])
        self.assertEqual(self.f.items()[0][2:], ('applying', 2))
        self.assertIsNone(port.read_ledger(P, lambda c: records._one(
            c, 'SELECT id FROM relays WHERE submission_key=?', (job.apply_key,))))

    def test_indeterminate_registered_refuses_without_success(self):
        before = self.f.snapshot()
        for fault in ('job_ended_transaction', 'rollback_failed'):
            bogus = api_writer.JobOutcome(False, {'receipt': 'bogus'}, True, fault)
            with (patch.object(self.writer, 'submit_declared', return_value=bogus) as submit,
                  patch.object(self.writer, 'read_catalogue', wraps=self.writer.read_catalogue) as read):
                response = self.dispatch('queue.answer', self.action(answer={'text': 'Answer'}),
                                         principal=PAGE).response
                self.assertEqual(response['error']['cause']['kind'], 'handler_fault')
                self.assertNotIn('result', response)
                submit.assert_called_once()
                read.assert_called_once()
        self.assertEqual(self.f.snapshot(), before)
