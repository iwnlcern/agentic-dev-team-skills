"""Queue occurrences, receipts and undo over fresh file-backed catalogues."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import api_principals, store_layout
from relay_engine.api_principals import OperatorPrincipal, ServicePrincipal
from relay_engine.api_protocol import SeatBinding
from relay_engine.jcs import jcs_encode
from relay_engine.operator_queue import kinds, records
from relay_engine.tests.test_catalogue import P, Q, QUEUE_TABLES


ALL = frozenset({'pr1', 'pr2', 'pr3', 'pr3c', 'pr4', 'pr5', 'pr6'})


def principal_constructions(package):
    found = []
    for path in sorted(package.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name) else
                        node.func.attr if isinstance(node.func, ast.Attribute) else None)
                if name in ('OperatorPrincipal', 'ServicePrincipal'):
                    found.append((path.name, node.lineno))
    return found


class FileJobPort:
    """The five dispatcher modes, with no production dispatcher substituted."""
    def __init__(self, root):
        self.root = root
        self.ledger = root / 'test-ledger.sqlite3'
        conn = sqlite3.connect(self.ledger)
        conn.execute('CREATE TABLE proof (value INTEGER)').close()
        conn.close()

    def run(self, path, fn, *, transaction=False, ledger_read=False):
        conn = None
        reader = None
        try:
            conn = sqlite3.connect(path, isolation_level=None, timeout=0.01)
            if transaction:
                conn.execute('BEGIN IMMEDIATE').close()
            if ledger_read:
                reader = sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True,
                                         isolation_level=None, timeout=0.01)
                result = fn(conn, reader)
            else:
                result = fn(conn)
            if transaction:
                conn.execute('COMMIT').close()
            return result
        except sqlite3.Error as exc:
            raise records.JobUnavailable(str(exc)) from exc
        finally:
            if reader is not None:
                reader.close()
            if conn is not None:
                conn.close()

    def catalogue(self, fn):
        return self.run(self.root / 'machine.sqlite3', fn, transaction=True)

    def catalogue_with_ledger_read(self, project_id, fn):
        return self.run(self.root / 'machine.sqlite3', fn, transaction=True, ledger_read=True)

    def callee_ledger(self, project_id, fn):
        return self.run(self.ledger, fn)

    def read_ledger(self, project_id, fn):
        return self.run(self.ledger, fn)

    def read_catalogue(self, fn):
        return self.run(self.root / 'machine.sqlite3', fn)


class TestQueueRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        self.conn = store_layout.open_catalogue(self.root)
        self.addCleanup(self.conn.close)
        for project, name in ((P, 'p'), (Q, 'q')):
            self.conn.execute('INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, ?, ?, ?)',
                              (project, name, 'active', 0)).close()
        self.conn.commit()
        self.internal = records.InternalWriter()
        self.authority = ServicePrincipal('authority_service')
        self.engine = ServicePrincipal('engine_liveness')
        self.transfer = ServicePrincipal('xproject_transfer')
        self.seat = SeatBinding(P, 'team', 'seat', 'occupant', 'key', 'session', 1)
        self.counter = 0

    def request(self, kind='notice', subkind='doorbell_off', project=P, key='key', **changes):
        self.counter += 1
        values = dict(kind=kind, subkind=subkind, project_id=project,
                      texts=({'subject': 'Question', 'body': 'Body'} if kind == 'question'
                             else {'sentence': 'Something happened.'}),
                      pins={}, condition_key=key, operation_key='op-' + str(self.counter))
        values.update(changes)
        return records.RaiseRequest(**values)

    def raise_it(self, principal=None, request=None, **kwargs):
        result = records.raise_item(self.conn, principal or self.engine,
                                   request or self.request(), 100, enabled=ALL, **kwargs)
        self.assertIsInstance(result, records.RaiseResult)
        return result

    def rows(self, table, where='', params=()):
        cursor = self.conn.execute('SELECT * FROM ' + table + where, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def snapshot(self):
        return {table: self.rows(table, ' ORDER BY rowid') for table in sorted(QUEUE_TABLES)}

    def refusal(self, principal, request, cause='subject_unresolved', code='E_QUEUE_INVALID'):
        before = self.snapshot()
        changes = self.conn.total_changes
        result = records.raise_item(self.conn, principal, request, 100, enabled=ALL)
        self.assertIsInstance(result, records.QueueRefusal)
        self.assertEqual((result.code, result.cause_kind), (code, cause))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.total_changes, changes)

    def clear(self, item, principal=None, request=None):
        request = request or self.request()
        return records.clear_condition(self.conn, principal or self.engine, item.item_id,
                                       request.kind, request.subkind, request.project_id,
                                       request.condition_key, 200)

    def supervision(self):
        # G-OPS adds this principal later; only this test fixture enables its matrix rows.
        with mock.patch.object(api_principals, 'SERVICE_NAMES',
                               (*api_principals.SERVICE_NAMES, 'supervision')):
            return ServicePrincipal('supervision')

    def test_raise_by_matrix_row(self):
        request = self.request('merge_go_ahead', None)
        result = self.raise_it(self.authority, request)
        for table in ('queue_items', 'queue_conditions', 'queue_events', 'queue_notifications'):
            self.assertEqual(len(self.rows(table)), 1)
        self.assertEqual(self.conn.execute('SELECT event_kind,principal_kind,principal_id,at_ms '
                                          'FROM queue_events').fetchone(),
                         ('raised', 'service', 'authority_service', 100))
        self.assertEqual(self.conn.execute('SELECT state,next_attempt_at_ms FROM queue_notifications')
                         .fetchone(), ('pending', 100))
        for principal, invalid in ((self.seat, request), (self.engine, request),
                                   (self.authority, self.request())):
            self.refusal(principal, invalid, 'kind_not_raisable', 'E_QUEUE_INVALID')
        self.assertEqual(result.revision, 1)

    def test_raise_stores_fixed_text(self):
        principal = self.supervision()
        request = self.request(subkind='worker_stalled', project=None, key='queue_reconcile',
                               texts={'sentence': 'Caller text.', 'subject': 'Drop me'})
        with mock.patch.object(kinds, 'FIXED_TEXTS', {('worker_stalled', 'queue_reconcile'): 'Fixed.'}):
            first = self.raise_it(principal, request)
        with mock.patch.object(kinds, 'FIXED_TEXTS', {}):
            self.clear(first, principal, request)
            second = self.raise_it(principal, replace(request, operation_key='second'))
        values = dict(self.conn.execute('SELECT item_id,texts FROM queue_items').fetchall())
        self.assertEqual(values[first.item_id], '{"sentence":"Fixed."}')
        self.assertEqual(values[second.item_id], jcs_encode(dict(request.texts)).decode())

    def test_texts_shape(self):
        invalid = [{}, {'title': 'x'}, {'sentence': 'x', 'title': 'x'},
                   {'sentence': 'x', 'body': 'x'}, {'sentence': ''},
                   {'sentence': 'x', 'subject': ''}, {'sentence': 'a\x00b'},
                   {'sentence': 'x', 'caused_by': 'x\x00'}, {'sentence': 'é' * 2048 + 'a'}]
        for texts in invalid:
            with self.subTest(texts=repr(texts)[:80]):
                self.refusal(self.engine, self.request(texts=texts))
        for index, texts in enumerate([{'sentence': 'x'},
                {'sentence': 'x', 'subject': 's', 'caused_by': 'c', 'ring_text': 'r'},
                {'sentence': 'é' * 2048}]):
            item = self.raise_it(request=self.request(key=str(index), texts=texts))
            self.assertEqual(self.conn.execute('SELECT texts FROM queue_items WHERE item_id=?',
                                              (item.item_id,)).fetchone()[0],
                             jcs_encode(texts).decode())

    def test_internal_rows_need_internal_writer(self):
        for request in (self.request('question', None),
                        self.request(subkind='notifier_unhealthy', project=None, key='notifier')):
            item = self.raise_it(self.internal, request)
            self.assertEqual(self.conn.execute('SELECT principal_kind,principal_id FROM queue_events '
                                               'WHERE item_id=?', (item.item_id,)).fetchone(),
                             ('service', 'console_queue'))
            self.refusal(ServicePrincipal('console_queue'), request,
                         'kind_not_raisable', 'E_QUEUE_INVALID')
        for request in (self.request('merge_go_ahead', None), self.request()):
            self.refusal(self.internal, request, 'kind_not_raisable', 'E_QUEUE_INVALID')

    def test_package_constructs_no_principal(self):
        package = Path(__file__).parents[1] / 'operator_queue'
        self.assertEqual(principal_constructions(package), [])
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / 'probe.py'
            for source in ('OperatorPrincipal("page")', 'api.ServicePrincipal("console_queue")'):
                probe.write_text(source)
                self.assertTrue(principal_constructions(Path(tmp)))

    def test_no_project_rule(self):
        self.refusal(self.internal, self.request(subkind='notifier_unhealthy', key='notifier'))
        self.refusal(self.internal, self.request(subkind='seat_registered', project=None))
        self.refusal(self.engine, self.request(subkind='notifier_unhealthy', project=None),
                     'kind_not_raisable', 'E_QUEUE_INVALID')
        self.conn.execute('DELETE FROM project_routes').close()
        self.raise_it(self.internal, self.request(subkind='notifier_unhealthy', project=None, key='notifier'))

    def test_row_key_rule(self):
        self.refusal(self.internal, self.request(subkind='notifier_unhealthy', project=None,
                                                key='notify_worker'))
        self.raise_it(self.internal, self.request(subkind='notifier_unhealthy', project=None, key='notifier'))

    def test_instructed_act_raised_only_internally(self):
        request = self.request(subkind='operator_act_carried')
        for principal in (self.engine, self.authority, self.transfer, ServicePrincipal('console_queue')):
            self.refusal(principal, request, 'kind_not_raisable', 'E_QUEUE_INVALID')
        self.raise_it(self.internal, request)
        self.assertEqual(self.rows('queue_notifications'), [])

    def test_actor_address_form(self):
        for actor in ('operator', 'alpha.implementer@other', 'alpha',
                      'a' * 65 + '.orchestrator-reviewer'):
            self.refusal(self.engine, self.request(actor_address=actor))
        for index, actor in enumerate(('a' * 64 + '.orchestrator-reviewer', 'alpha.implementer')):
            item = self.raise_it(request=self.request(key=str(index), actor_address=actor))
            self.assertEqual(self.conn.execute('SELECT actor_address FROM queue_items WHERE item_id=?',
                                              (item.item_id,)).fetchone(), (actor,))

    def test_actor_address_is_display_only(self):
        request = self.request(actor_address='alpha.implementer')
        first = self.raise_it(request=request)
        before = self.snapshot()
        second = self.raise_it(request=replace(request, actor_address='beta.implementer'))
        self.assertTrue(second.replayed)
        self.assertEqual(second.item_id, first.item_id)
        self.assertEqual(self.snapshot(), before)
        # Re-create the same ids/clock against the same identity, varying only the actor.
        snapshots = []
        for actor in (None, 'alpha.implementer'):
            self.conn.execute('SAVEPOINT actor').close()
            with mock.patch.object(records, 'new_uuid7', side_effect=['item-control', 'receipt-control']):
                self.raise_it(request=replace(request, condition_key='other', operation_key='other',
                                              actor_address=actor))
            snapshots.append(self.snapshot())
            self.conn.execute('ROLLBACK TO actor').close()
            self.conn.execute('RELEASE actor').close()
        items = snapshots[1]['queue_items']
        actor_index = [r[1] for r in self.conn.execute('PRAGMA table_info(queue_items)')].index('actor_address')
        snapshots[1]['queue_items'] = [tuple(None if i == actor_index else v for i, v in enumerate(row))
                                      if row[0] == 'item-control' else row for row in items]
        self.assertEqual(*snapshots)

    def test_fields_written_at_raise_only(self):
        request = self.request(subkind='notifier_unhealthy', project=None, key='notifier')
        first = self.raise_it(self.internal, request, fields={'a': 1})
        self.raise_it(self.internal, replace(request, operation_key='second'), fields={'a': 2})
        self.assertEqual(self.conn.execute('SELECT fields FROM queue_items WHERE item_id=?',
                                          (first.item_id,)).fetchone(), ('{"a":1}',))
        other = self.raise_it(self.internal, self.request('question', None))
        self.assertEqual(self.conn.execute('SELECT fields FROM queue_items WHERE item_id=?',
                                          (other.item_id,)).fetchone(), (None,))

    def test_repeat_raise_returns_open_item(self):
        for index, state in enumerate(sorted(kinds.STATES)):
            request = self.request(key=str(index))
            first = self.raise_it(request=request)
            self.conn.execute('UPDATE queue_items SET state=? WHERE item_id=?', (state, first.item_id)).close()
            before = self.snapshot()
            second = self.raise_it(request=replace(request, operation_key='repeat-' + str(index)))
            self.assertEqual(second.item_id, first.item_id)
            for table in QUEUE_TABLES - {'queue_receipts'}:
                self.assertEqual(self.snapshot()[table], before[table])

    def test_raise_receipt_replay(self):
        request = self.request()
        first = self.raise_it(request=request)
        before = self.snapshot()
        again = self.raise_it(request=request)
        self.assertEqual((again.item_id, again.revision, again.state, again.receipt_id),
                         (first.item_id, first.revision, first.state, first.receipt_id))
        self.assertTrue(again.replayed)
        self.assertEqual(self.snapshot(), before)
        self.refusal(self.engine, replace(request, texts={'sentence': 'Changed.'}),
                     'operation_key_reused', 'E_API_CONFLICT')

    def test_clear_pending_closes(self):
        item = self.raise_it()
        result = self.clear(item)
        self.assertEqual(result.outcome, 'cleared')
        self.assertEqual(self.conn.execute('SELECT revision,state FROM queue_items').fetchone(), (2, 'cleared'))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT event_kind FROM queue_events ORDER BY position')],
                         ['raised', 'cleared', 'condition_closed'])
        self.assertEqual(self.conn.execute('SELECT closed_at_ms FROM queue_conditions').fetchone(), (200,))

    def test_clear_applying_sets_flag_once(self):
        item = self.raise_it()
        self.conn.execute("UPDATE queue_items SET state='applying' WHERE item_id=?", (item.item_id,)).close()
        self.assertEqual(self.clear(item).outcome, 'clear_pending')
        before = self.snapshot()
        self.assertEqual(self.clear(item).outcome, 'clear_pending')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('SELECT revision,state FROM queue_items').fetchone(), (1, 'applying'))
        self.assertEqual(self.conn.execute('SELECT clear_requested FROM queue_conditions').fetchone(), (1,))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM queue_events WHERE event_kind='clear_requested'")
                         .fetchone(), (1,))

    def test_clear_names_its_occurrence(self):
        request = self.request()
        first = self.raise_it(request=request)
        self.clear(first, request=request)
        second = self.raise_it(request=replace(request, operation_key='second'))
        before = self.snapshot()
        self.assertEqual(self.clear(first, request=request).outcome, 'already_closed')
        self.assertEqual(self.snapshot(), before)
        self.assertNotEqual(first.item_id, second.item_id)
        self.assertEqual(self.clear(second, request=request).outcome, 'cleared')

    def test_clear_mismatched_key_refuses(self):
        item = self.raise_it()
        for principal, request in ((self.engine, self.request(key='wrong')),
                                   (self.authority, self.request())):
            before = self.snapshot()
            result = self.clear(item, principal, request)
            self.assertIsInstance(result, records.QueueRefusal)
            self.assertEqual(result.cause_kind, 'subject_unresolved')
            self.assertEqual(self.snapshot(), before)

    def test_no_project_condition_recovered(self):
        request = self.request(subkind='notifier_unhealthy', project=None, key='notifier')
        item = self.raise_it(self.internal, request)
        rows = records.open_condition_items(self.conn, self.internal, None)
        self.assertEqual([(r.kind, r.subkind, r.condition_key, r.item_id) for r in rows],
                         [('notice', 'notifier_unhealthy', 'notifier', item.item_id)])
        self.assertEqual(records.open_condition_items(self.conn, self.engine, None), [])
        self.assertIsInstance(self.clear(item, self.engine, request), records.QueueRefusal)
        self.assertEqual(self.clear(item, self.internal, request).outcome, 'cleared')

    def test_opened_doorbell_does_not_ring_again(self):
        request = self.request()
        item = self.raise_it(request=request)
        # Task 4 owns dismiss; this fixture starts at its committed resulting state.
        self.conn.execute("UPDATE queue_items SET state='dismissed',revision=2 WHERE item_id=?", (item.item_id,)).close()
        before = self.snapshot()
        again = self.raise_it(request=replace(request, operation_key='again'))
        self.assertEqual(again.item_id, item.item_id)
        self.assertEqual(self.rows('queue_notifications'), before['queue_notifications'])
        self.clear(item, request=request)
        new = self.raise_it(request=replace(request, operation_key='new'))
        self.assertNotEqual(new.item_id, item.item_id)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM queue_notifications WHERE item_id=?',
                                          (new.item_id,)).fetchone(), (1,))

    def test_open_condition_items_own_rows_only(self):
        doorbell = self.raise_it()
        merge = self.raise_it(self.authority, self.request('merge_go_ahead', None))
        self.assertEqual([r.item_id for r in records.open_condition_items(self.conn, self.engine, P)], [doorbell.item_id])
        self.assertEqual([r.item_id for r in records.open_condition_items(self.conn, self.authority, P)], [merge.item_id])

    def test_seal_condition_matrix(self):
        from relay_engine.operator_queue import notice_text
        from relay_engine.tests.test_queue_notice_text import POINTER_SUFFIX, SEAL_CAUSES

        seal = ServicePrincipal('approval_seal')
        for cause, (chain, _) in SEAL_CAUSES.items():
            with self.subTest(cause=cause):
                key = dict(chain_id=chain, cause=cause)
                request = self.request(subkind='approval_seal_unusable', key=key, pins=key,
                                       texts=notice_text.seal_texts(cause, chain))
                item = self.raise_it(seal, request)
                saved = json.loads(self.conn.execute('SELECT texts FROM queue_items WHERE item_id=?',
                                                     (item.item_id,)).fetchone()[0])
                self.assertEqual(saved['sentence'].endswith(POINTER_SUFFIX), cause == 'pointer_damaged')
                before = self.snapshot()
                self.assertEqual(self.raise_it(seal, replace(request, operation_key='repeat-' + cause)).item_id,
                                 item.item_id)
                for table in ('queue_items', 'queue_conditions', 'queue_events', 'queue_notifications'):
                    self.assertEqual(self.snapshot()[table], before[table])
                self.assertEqual(self.clear(item, seal, request).outcome, 'cleared')
        for chain, cause in [('chain', 'unknown'), ('none', 'counter_unusable'),
                             *[('chain', cause) for cause in ('folder_unusable', 'pointer_unset', 'pointer_damaged')]]:
            self.refusal(seal, self.request(subkind='approval_seal_unusable',
                                           key=dict(chain_id=chain, cause=cause)))
        request = self.request(subkind='approval_seal_unusable',
                               key=dict(chain_id='none', cause='folder_unusable'))
        for principal in (self.authority, ServicePrincipal('console_queue'), self.internal, self.seat):
            self.refusal(principal, request, 'kind_not_raisable')
        self.refusal(seal, self.request(), 'kind_not_raisable')

    def backup_request(self, **changes):
        return self.request(subkind='approval_seal_backup_included', project=None,
                            key='backup_exclusion', **changes)

    def test_backup_notice_matrix(self):
        seal = ServicePrincipal('approval_seal')
        self.conn.execute('DELETE FROM project_routes').close()
        self.assertEqual(self.rows('project_routes'), [])
        request = self.backup_request()
        item = self.raise_it(seal, request)
        self.assertEqual(len(self.rows('queue_items')), 1)
        self.assertEqual(len(records.open_condition_items(self.conn, seal, None)), 1)
        before = self.snapshot()
        self.assertEqual(self.raise_it(seal, replace(request, operation_key='again')).item_id, item.item_id)
        for table in ('queue_items', 'queue_conditions', 'queue_events', 'queue_notifications'):
            self.assertEqual(self.snapshot()[table], before[table])
        self.assertEqual(self.clear(item, seal, request).outcome, 'cleared')
        self.refusal(seal, replace(request, project_id=P))
        self.refusal(seal, replace(request, condition_key='other'))
        for principal in (self.authority, ServicePrincipal('console_queue'), self.internal, self.seat):
            self.refusal(principal, request, 'kind_not_raisable')
        self.refusal(self.internal, self.request(subkind='seat_registered', project=None))
        self.refusal(self.authority, self.request(kind='merge_go_ahead', subkind=None, project=None))

    def test_no_project_untouched_by_undo(self):
        seal = ServicePrincipal('approval_seal')
        machine = self.raise_it(seal, self.backup_request())
        doorbell = self.raise_it()
        before = self.snapshot()
        result = records.undo_step(self.conn, P, 'undo', now_ms=300)
        self.assertEqual(result.retired_item_ids, (doorbell.item_id,))
        self.assertEqual(result.closed_condition_item_ids, (doorbell.item_id,))
        for table in ('queue_items', 'queue_conditions', 'queue_notifications'):
            self.assertEqual(self.rows(table, ' WHERE item_id=?', (machine.item_id,)),
                             [row for row in before[table] if row[0] == machine.item_id])
        self.assertEqual([row.item_id for row in records.open_condition_items(self.conn, seal, None)],
                         [machine.item_id])
        self.assertEqual(records.open_condition_items(self.conn, self.engine, P), [])

    def test_no_project_recover_and_clear(self):
        seal = ServicePrincipal('approval_seal')
        request = self.backup_request()
        item = self.raise_it(seal, request)
        self.assertEqual([row.item_id for row in records.open_condition_items(self.conn, seal, None)],
                         [item.item_id])
        self.assertEqual(records.open_condition_items(self.conn, self.engine, None), [])
        before = self.snapshot()
        self.assertEqual(self.clear(item, self.engine, request).cause_kind, 'subject_unresolved')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.clear(item, seal, request).outcome, 'cleared')
        self.assertEqual(records.open_condition_items(self.conn, seal, None), [])

    def test_undo_retires_and_closes(self):
        a = self.raise_it()
        b = self.raise_it(self.internal, self.request(subkind='seat_registered', key='seat'))
        c = self.raise_it(request=self.request(key='dismissed'))
        self.conn.execute("UPDATE queue_items SET state='dismissed' WHERE item_id=?", (c.item_id,)).close()
        other = self.raise_it(request=self.request(project=Q))
        machine = self.raise_it(self.internal, self.request(subkind='notifier_unhealthy', project=None, key='notifier'))
        records.advance_cursor(self.conn, Q, 'question', records.Cursor(0, 0, None), 2, anchor_id='q')
        records.advance_cursor(self.conn, None, 'lifecycle_log', records.Cursor(0, 0, None), 2, anchor_id='machine')
        before = self.snapshot()
        result = records.undo_step(self.conn, P, 'undo', now_ms=300)
        self.assertEqual(set(result.retired_item_ids), {a.item_id, b.item_id})
        self.assertEqual(set(result.closed_condition_item_ids), {a.item_id, b.item_id, c.item_id})
        self.assertEqual((result.retired_outbox, result.epoch), (2, 1))
        for source in ('question', 'lifecycle'):
            self.assertEqual(records.read_cursor(self.conn, P, source), records.Cursor(0, 1, None))
        for item in (other, machine):
            self.assertEqual(self.rows('queue_items', ' WHERE item_id=?', (item.item_id,)),
                             [r for r in before['queue_items'] if r[0] == item.item_id])
            self.assertEqual(self.rows('queue_conditions', ' WHERE item_id=?', (item.item_id,)),
                             [r for r in before['queue_conditions'] if r[0] == item.item_id])
            self.assertEqual(self.rows('queue_notifications', ' WHERE item_id=?', (item.item_id,)),
                             [r for r in before['queue_notifications'] if r[0] == item.item_id])
        for source, project, anchor in (('question', Q, 'q'), ('lifecycle_log', None, 'machine')):
            self.assertEqual(records.read_cursor(self.conn, project, source), records.Cursor(2, 0, anchor))
        retired = self.conn.execute("SELECT principal_kind,principal_id,detail FROM queue_events WHERE event_kind='retired'").fetchall()
        self.assertEqual(len(retired), 2)
        for kind, principal, detail in retired:
            self.assertEqual((kind, principal), ('service', 'console_queue'))
            self.assertEqual(json.loads(detail), {'reason': 'project_undone', 'undo_job_id': 'undo'})

    def test_undo_blocked_by_permission(self):
        item = self.raise_it(self.transfer, self.request('cross_project_permission', None, project=Q,
                            key={'relay_id': 'relay', 'body_sha256': 'a' * 64, 'receiver_project_id': P}))
        for state in ('pending', 'applying'):
            self.conn.execute('UPDATE queue_items SET state=? WHERE item_id=?', (state, item.item_id)).close()
            before, count = self.snapshot(), self.conn.total_changes
            with self.assertRaises(records.UndoBlocked) as caught:
                records.undo_step(self.conn, P, 'undo', now_ms=300)
            self.assertEqual([(r.item_id, r.names_as) for r in caught.exception.items], [(item.item_id, 'destination')])
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.conn.total_changes, count)
        self.conn.execute('UPDATE queue_items SET condition_key=? WHERE item_id=?',
                          (kinds.condition_key('cross_project_permission', None,
                            {'receiver_project_id': Q}), item.item_id)).close()
        records.undo_step(self.conn, P, 'undo', now_ms=300)

    def test_undo_caller_owns_transaction(self):
        self.raise_it()
        before = self.snapshot()
        self.conn.execute('SAVEPOINT undo').close()
        trace = []
        self.conn.set_trace_callback(trace.append)
        records.undo_step(self.conn, P, 'undo', now_ms=300)
        self.conn.set_trace_callback(None)
        self.assertFalse(any(sql.split()[0].upper() in ('BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE') for sql in trace))
        self.conn.execute('ROLLBACK TO undo').close()
        self.conn.execute('RELEASE undo').close()
        self.assertEqual(self.snapshot(), before)
        self.conn.execute('DELETE FROM project_routes').close()
        self.raise_it(self.internal, self.request(subkind='notifier_unhealthy', project=None, key='notifier'))
        self.refusal(self.engine, self.request(), 'project_not_active', 'E_QUEUE_STATE')

    def test_undo_epoch_on_empty_queue(self):
        self.assertEqual(records.undo_step(self.conn, P, 'first', now_ms=300).epoch, 1)
        self.assertEqual(records.undo_step(self.conn, P, 'second', now_ms=300).epoch, 2)
        for source in ('question', 'lifecycle'):
            self.assertEqual(records.read_cursor(self.conn, P, source), records.Cursor(0, 2, None))

    def test_raise_refuses_inactive_project(self):
        self.conn.execute("UPDATE project_routes SET state='creating' WHERE project_id=?", (P,)).close()
        self.refusal(self.engine, self.request(), 'project_not_active', 'E_QUEUE_STATE')
        self.conn.execute("UPDATE project_routes SET state='active' WHERE project_id=?", (P,)).close()
        self.raise_it()

    def test_raise_destination_fence(self):
        self.conn.execute("UPDATE project_routes SET state='creating' WHERE project_id=?", (P,)).close()
        self.refusal(self.transfer, self.request('cross_project_permission', None, project=Q,
                     key={'receiver_project_id': P}), 'project_not_active', 'E_QUEUE_STATE')
        self.raise_it(self.transfer, self.request('cross_project_permission', None, project=Q,
                      key={'receiver_project_id': Q}))

    def test_event_positions_strictly_increase(self):
        for index in range(4):
            request = self.request(key=str(index))
            self.clear(self.raise_it(request=request), request=request)
        self.assertEqual([row[0] for row in self.conn.execute('SELECT position FROM queue_events ORDER BY position')], list(range(1, 13)))

    def test_job_port_fake_matches_modes(self):
        port = FileJobPort(self.root)
        self.assertTrue(port.catalogue(lambda conn: conn.in_transaction))
        self.assertEqual(port.catalogue_with_ledger_read(P, lambda conn, ledger:
                         (conn.in_transaction, ledger.in_transaction)), (True, False))
        self.assertFalse(port.callee_ledger(P, lambda conn: conn.in_transaction))
        self.assertFalse(port.read_catalogue(lambda conn: conn.in_transaction))
        self.assertFalse(port.read_ledger(P, lambda conn: conn.in_transaction))
        lock = sqlite3.connect(self.root / 'machine.sqlite3', isolation_level=None)
        self.addCleanup(lock.close)
        lock.execute('BEGIN IMMEDIATE').close()
        with self.assertRaises(records.JobUnavailable):
            port.catalogue(lambda conn: None)
        lock.execute('ROLLBACK').close()
        broken = FileJobPort.__new__(FileJobPort)
        broken.root = self.root / 'missing'
        with self.assertRaises(records.JobUnavailable):
            broken.read_catalogue(lambda conn: None)
        self.assertTrue(all(hasattr(records.JobPort, name) for name in
                        ('catalogue', 'catalogue_with_ledger_read', 'callee_ledger', 'read_ledger', 'read_catalogue')))

    def test_pin_token_fill(self):
        request = self.request(subkind='worker_stalled', project=None, key='queue_reconcile', pins={'x': '[<t>]'})
        with mock.patch.object(kinds, 'FIXED_TEXTS', {('worker_stalled', '*'): 'a <t> b'}):
            with mock.patch.object(kinds, 'PIN_TOKENS', {'worker_stalled': ('<t>', 'x')}):
                first = self.raise_it(self.supervision(), request)
            self.clear(first, self.supervision(), request)
            with mock.patch.object(kinds, 'PIN_TOKENS', {}):
                second = self.raise_it(self.supervision(), replace(request, operation_key='second'))
        self.assertEqual(self.conn.execute('SELECT texts FROM queue_items WHERE item_id=?', (first.item_id,)).fetchone(), ('{"sentence":"a [<t>] b"}',))
        self.assertEqual(self.conn.execute('SELECT texts FROM queue_items WHERE item_id=?', (second.item_id,)).fetchone(), ('{"sentence":"a <t> b"}',))

    def test_cursor_compare_and_swap(self):
        empty = records.Cursor(0, 0, None)
        self.assertEqual(records.read_cursor(self.conn, P, 'question'), empty)
        self.assertTrue(records.advance_cursor(self.conn, P, 'question', empty, 2, anchor_id='a'))
        for expected in (empty, records.Cursor(2, 1, 'a'), records.Cursor(2, 0, 'wrong')):
            before = self.snapshot()
            self.assertFalse(records.advance_cursor(self.conn, P, 'question', expected, 3, anchor_id='b'))
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(records.advance_cursor(self.conn, P, 'question', records.Cursor(2, 0, 'a'), 3, anchor_id='b'))
        self.assertTrue(records.advance_cursor(self.conn, None, 'stop_events', empty, 1))
        self.assertEqual(records.read_cursor(self.conn, None, 'stop_events'), records.Cursor(1, 0, None))


if __name__ == '__main__':
    unittest.main()
