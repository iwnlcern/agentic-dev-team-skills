"""Store-level lifecycle sources, skip records and pre-join drain semantics."""
from datetime import timedelta, timezone
import json
import sqlite3
import unittest
from unittest import mock

from relay_engine import api_writer, store_items, store_layout, store_steps
from relay_engine.jcs import jcs_encode
from relay_engine.operator_queue import component, ingest, notice_text, notifier, operations, records
from relay_engine.tests.test_catalogue import P, Q
from relay_engine.tests.test_queue_ingest import QueueFixture, Port


class StorePort(Port):
    def read_ledger(self, project, fn):
        raise AssertionError('store-level ingest opened a project ledger')


class DeclaredStorePort(StorePort):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.calls = []
        self.outcomes = []
        self.before_submit = None
        self.unavailable = False

    def read_catalogue(self, fn):
        self.calls.append(('read_catalogue',))
        return super().read_catalogue(fn)

    def submit_declared(self, job):
        self.calls.append(('submit_declared', job.declared))
        if self.unavailable:
            raise api_writer.WriterUnavailable('store_held')
        if self.before_submit is not None:
            before, self.before_submit = self.before_submit, None
            before()
        conn = self.f.catalogue
        conn.execute('BEGIN IMMEDIATE').close()
        try:
            outcome = job.run({'catalogue': conn})
            self.outcomes.append(outcome)
            (conn.commit if outcome.commit else conn.rollback)()
            return outcome
        except BaseException:
            conn.rollback()
            raise


class TestStoreIngest(unittest.TestCase):
    def setUp(self):
        self.f = QueueFixture(self)
        self.c = self.f.catalogue
        self.port = StorePort(self.f)
        self.log = self.f.root / 'lifecycle.jsonl'
        self.assertTrue(callable(getattr(ingest, 'store_ingest_pass', None)),
                        'store-level lifecycle ingest is absent')

    def line(self, seq, kind='outage', **fields):
        defaults = (dict(started_at='2026-11-15T00:00:00Z', ended_at='2026-11-15T01:00:00Z')
                    if kind == 'outage' else dict(outcome='updated', **{'from':'old', 'to':'new'}))
        defaults.update(fields)
        record = dict(format='adt-lifecycle-v1', seq=seq, id='id-'+str(seq), kind=kind,
                      project_id=None, at='2026-11-15T01:00:00Z')
        record.update(defaults)
        return record

    def write(self, *lines):
        self.log.write_bytes(b''.join(json.dumps(line).encode()+b'\n' for line in lines))

    def run_pass(self):
        return ingest.store_ingest_pass(self.port, self.log, self.f.clock).raised

    def items(self):
        return self.c.execute('SELECT subkind,project_id,texts,pins FROM queue_items ORDER BY raised_position').fetchall()

    def cursor(self, source):
        return records.read_cursor(self.c, None, source)

    def skips(self):
        return self.c.execute('SELECT source,row_ref,reason FROM queue_ingest_skips ORDER BY row_ref').fetchall()

    def stop(self, scope='store', project=None):
        result = store_layout.record_stop(self.c, scope, project, 'operator')
        self.c.commit()
        return result

    def start(self, scope='store', project=None):
        result = store_layout.record_start(self.c, scope, project, 'operator')
        self.c.commit()
        return result

    def test_log_raises_outage_and_update(self):
        self.write(self.line(1), self.line(2, 'update'))
        self.assertEqual(self.run_pass(), 2)
        self.assertEqual([r[:2] for r in self.items()], [('engine_was_down',None), ('engine_updated',None)])
        for subkind, project, texts, pins in self.items():
            self.assertEqual(texts, jcs_encode({'sentence':notice_text.SERVICE_SENTENCES[subkind]}).decode())
        self.assertEqual(json.loads(self.items()[0][3]),
                         dict(started_at='2026-11-15T00:00:00Z', ended_at='2026-11-15T01:00:00Z'))
        self.assertEqual(json.loads(self.items()[1][3]),
                         {'at':'2026-11-15T01:00:00Z','from':'old','to':'new'})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM queue_notifications').fetchone()[0], 0)
        self.assertEqual(self.run_pass(), 0)

    def test_log_malformed_record_skipped(self):
        malformed = self.line(1); del malformed['ended_at']
        valid = self.line(2, 'update')
        self.write(malformed, valid)
        with mock.patch.object(ingest, 'log_notice', side_effect=KeyError('ended_at')):
            with self.assertRaises(KeyError):
                self.run_pass()
        self.assertEqual(self.items(), [])
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.skips(), [('lifecycle_log','id-1','missing_field:ended_at')])
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(2,0,'id-2'))
        malformed['seq']=11; valid['seq']=12
        self.write(malformed, valid)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(len(self.skips()), 1)
        self.write({'format':'adt-lifecycle-v1','id':'unpositioned'})
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(12,0,'id-2'))

    def test_log_stale_cursor_writes_nothing(self):
        self.write(self.line(1))
        lines = ingest.read_lifecycle_log(self.log.read_bytes())
        cursor = self.cursor('lifecycle_log')
        self.assertEqual(self.run_pass(), 1)
        before = self.f.snapshot()
        self.assertIsInstance(self.port.catalogue(lambda c: ingest.ingest_log(
            c, lines, cursor, self.f.clock)), records.Stale)
        self.assertEqual(self.f.snapshot(), before)
        self.write(self.line(1), self.line(2))
        current = self.cursor('lifecycle_log')
        self.assertEqual(self.port.catalogue(lambda c: ingest.ingest_log(
            c, ingest.read_lifecycle_log(self.log.read_bytes()), current, self.f.clock)), 1)

    def test_startup_fails_on_unavailable_source(self):
        self.stop(); self.write(self.line(1))
        joined=[]
        def hook():
            ingest.store_ingest_startup(self.port, self.log, self.f.clock)
            joined.append(P)
        with mock.patch.object(ingest, 'ingest_stop_events', side_effect=records.JobUnavailable('busy')):
            with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
                hook()
        self.assertEqual((caught.exception.source,caught.exception.cause), ('stop_events','job_unavailable'))
        self.assertEqual(joined, [])
        with mock.patch.object(ingest, 'ingest_log', return_value=records.Stale()):
            with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
                hook()
            self.assertEqual((caught.exception.source,caught.exception.cause), ('lifecycle_log','stale'))
            self.assertEqual(joined, [])
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual([r[0] for r in self.items()], ['engine_was_down','engine_stopped'])

    def test_startup_drains_to_the_end(self):
        self.write(*(self.line(n) for n in range(1,6)))
        self.stop(); last = self.start()
        self.assertEqual(ingest.store_ingest_startup(self.port, self.log, self.f.clock), 7)
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(5,0,'id-5'))
        self.assertEqual(self.cursor('stop_events').position, last)
        self.assertEqual(ingest.store_ingest_startup(self.port, self.log, self.f.clock), 0)
        self.assertEqual(len(self.items()), 7)

    def test_log_empty_id_is_absent(self):
        malformed = self.line(1, id=''); del malformed['ended_at']
        self.write(malformed,self.line(2,'update'))
        self.assertEqual(len(ingest.read_lifecycle_log(self.log.read_bytes())), 1)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.skips(), [])
        self.assertEqual(self.cursor('lifecycle_log').position, 2)
        malformed['id']='nonempty'
        self.write(malformed,self.line(3,'update'))
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.skips(), [('lifecycle_log','nonempty','missing_field:ended_at')])
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(len(self.skips()), 1)

    def test_store_pass_skips_one_source(self):
        self.write(self.line(1)); self.stop()
        with mock.patch.object(ingest, 'ingest_log', return_value=records.Stale()):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual([r[0] for r in self.items()], ['engine_stopped'])
        with mock.patch.object(ingest, 'ingest_stop_events', side_effect=records.JobUnavailable('busy')):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual([r[0] for r in self.items()], ['engine_stopped','engine_was_down'])
        self.write(self.line(2))
        with mock.patch.object(ingest, 'ingest_stop_events', return_value=records.Stale()):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual(len(self.items()), 3)

    def test_log_fragment_is_absent(self):
        one = json.dumps(self.line(1)).encode()
        self.log.write_bytes(one)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(0,0,None))
        self.log.write_bytes(one+b'\n')
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.run_pass(), 0)

    def test_log_replaced_with_shorter(self):
        self.write(self.line(1),self.line(2)); self.run_pass()
        new = self.line(1,id='new')
        old_cursor = self.cursor('lifecycle_log')
        rows = ingest.read_lifecycle_log((json.dumps(new)+'\n').encode())
        # Control: a reader filtering only by the old position drops the replacement.
        self.assertEqual([row for row in rows if row.seq > old_cursor.position], [])
        self.write(new)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(1,0,'new'))
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(len(self.items()), 3)

    def test_log_failed_update(self):
        self.write(self.line(1,'update',outcome='failed'))
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.items()[0][0], 'engine_update_failed')
        self.assertEqual(json.loads(self.items()[0][3]), {'at':'2026-11-15T01:00:00Z','from':'old','to':'new'})

    def test_stop_events_raise_by_scope(self):
        self.stop(); self.start(); self.stop('project',P); last=self.start('project',P)
        self.assertEqual(self.run_pass(), 4)
        self.assertEqual([row[:2] for row in self.items()],
                         [('engine_stopped',None),('engine_started',None),('project_stopped',P),('project_started',P)])
        for kind, project, text, pins in self.items():
            self.assertEqual(text, jcs_encode({'sentence':notice_text.SERVICE_SENTENCES[kind]}).decode())
            self.assertEqual(set(json.loads(pins)), {'at','by'})
        self.assertEqual(self.cursor('stop_events').position, last)
        self.assertEqual(self.run_pass(), 0)

    def test_stop_event_route_fence(self):
        self.c.execute("UPDATE project_routes SET state='creating' WHERE project_id=?",(P,)).close()
        stopped=self.stop('project',P)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.cursor('stop_events').position, stopped)
        self.c.execute("UPDATE project_routes SET state='active' WHERE project_id=?",(P,)).close()
        started=self.start('project',P)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.items()[0][0], 'project_started')
        self.assertEqual(self.cursor('stop_events').position, started)

    def test_stop_events_read_by_event_id(self):
        self.stop('project',P); self.stop()
        self.assertEqual(self.run_pass(), 2)
        old_position = self.cursor('stop_events').position
        last = self.start('project',P)
        wrong = self.c.execute('SELECT row_id FROM engine_stops WHERE row_id>?',(old_position,)).fetchall()
        self.assertEqual(wrong, [])
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.items()[-1][0], 'project_started')
        self.assertEqual(self.cursor('stop_events').position, last)

    def test_store_cursors_have_no_project(self):
        self.write(self.line(1)); self.stop(); self.run_pass()
        before = self.c.execute("SELECT project_id,source,position,epoch,anchor_id FROM queue_highwaters "
                                "WHERE project_id IS NULL ORDER BY source").fetchall()
        records.undo_step(self.c,P,'undo',now_ms=self.f.clock)
        after = self.c.execute("SELECT project_id,source,position,epoch,anchor_id FROM queue_highwaters "
                               "WHERE project_id IS NULL ORDER BY source").fetchall()
        self.assertEqual(before,after)
        self.assertEqual([row[:2] for row in before], [(None,'lifecycle_log'),(None,'stop_events')])

    def test_reader_ignores_unpositioned_and_keeps_bad_positioned(self):
        data = b'bad\n'+json.dumps(self.line(True)).encode()+b'\n'+json.dumps(self.line(1,kind=None)).encode()+b'\n'
        rows = ingest.read_lifecycle_log(data)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0].seq,rows[0].id,rows[0].kind),(1,'id-1',None))
        self.assertEqual(ingest.log_notice(rows[0]), 'unknown_kind:None')
        self.assertEqual(ingest.read_lifecycle_log(b'\xff\n'), ())

    def test_log_bad_field_and_integer_pins(self):
        self.write(self.line(1,started_at=True),self.line(2,'update',at=2**53))
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.skips(), [('lifecycle_log','id-1','bad_field:started_at')])
        self.assertEqual(json.loads(self.items()[0][3])['at'], str(2**53))

    def test_log_unencodable_value_skipped(self):
        self.write(self.line(1, 'update', **{'from': '\ud800'}),
                   self.line(2, kind='\ud800'),
                   self.line(3, 'update', to='v3-\u00e9'))
        stopped = self.stop()
        result = ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual((result.raised, result.log_unreadable), (2, None))
        self.assertEqual(self.skips(), [('lifecycle_log', 'id-1', 'bad_field:from'),
                                       ('lifecycle_log', 'id-2', 'bad_field:kind')])
        self.assertEqual([row[0] for row in self.items()], ['engine_updated', 'engine_stopped'])
        self.assertEqual(json.loads(self.items()[0][3])['to'], 'v3-\u00e9')
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(3, 0, 'id-3'))
        self.assertEqual(self.cursor('stop_events').position, stopped)
        ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual(len(self.items()), 2)
        self.assertEqual(len(self.skips()), 2)

    def test_log_unencodable_id_is_absent(self):
        self.write(self.line(1, id='\ud800'), self.line(2, 'update'))
        lines = ingest.read_lifecycle_log(self.log.read_bytes())
        self.assertEqual([line.id for line in lines], ['id-2'])
        ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual([row[0] for row in self.items()], ['engine_updated'])
        self.assertEqual(self.skips(), [])
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(2, 0, 'id-2'))

    def test_store_pass_skips_unreadable_log(self):
        self.log.mkdir()
        stopped = self.stop()
        before = self.cursor('lifecycle_log')
        result = ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual((result.raised, result.log_unreadable), (1, 'IsADirectoryError'))
        self.assertEqual([row[0] for row in self.items()], ['engine_stopped'])
        self.assertEqual(self.cursor('stop_events').position, stopped)
        self.assertEqual(self.cursor('lifecycle_log'), before)
        self.assertEqual(self.skips(), [])
        self.log.rmdir()
        self.write(self.line(1))
        result = ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual((result.raised, result.log_unreadable), (1, None))
        self.assertEqual([row[0] for row in self.items()], ['engine_stopped', 'engine_was_down'])
        self.assertEqual(self.cursor('lifecycle_log'), records.Cursor(1, 0, 'id-1'))

    def test_startup_unreadable_log_raises(self):
        self.log.mkdir()
        self.stop()
        before = self.f.snapshot()
        with self.assertRaises(IsADirectoryError):
            ingest.store_ingest_startup(self.port, self.log, self.f.clock)
        self.assertEqual(self.f.snapshot(), before)
        self.assertEqual(self.items(), [])

    def test_store_pass_result_names_no_fault(self):
        self.write(self.line(1))
        result = ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual((result.raised, result.log_unreadable), (1, None))
        self.log.unlink()
        result = ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual((result.raised, result.log_unreadable), (0, None))

    def test_store_pass_catalogue_oserror_propagates(self):
        self.write(self.line(1))
        self.stop()
        before = self.f.snapshot()
        catalogue = self.port.catalogue
        calls = 0
        def log_job_fault(fn):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('catalogue fault')
            return catalogue(fn)
        with mock.patch.object(self.port, 'catalogue', side_effect=log_job_fault):
            with self.assertRaisesRegex(OSError, 'catalogue fault'):
                ingest.store_ingest_pass(self.port, self.log, self.f.clock)
        self.assertEqual(self.f.snapshot(), before)

    def test_startup_callback_adapts_the_store_port(self):
        self.stop()
        self.write(self.line(1))
        port = DeclaredStorePort(self.f)
        callback = ingest.startup_callback(self.log)
        self.assertIsNone(callback(port))
        self.assertEqual([row[0] for row in self.items()], ['engine_was_down', 'engine_stopped'])
        submissions = [args for kind, *args in port.calls if kind == 'submit_declared']
        self.assertTrue(submissions)
        self.assertTrue(all(args == [store_steps.Declared('written', {})] for args in submissions))
        self.assertEqual({call[0] for call in port.calls}, {'submit_declared', 'read_catalogue'})
        self.assertTrue(all(outcome.commit for outcome in port.outcomes))
        port.unavailable = True
        with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
            callback(port)
        self.assertEqual(caught.exception.cause, 'job_unavailable')
        wrapped = component.catalogue_only(component.PlatformJobPort.over_port(port))
        before = port.calls[:]
        for forbidden in (lambda: wrapped.read_ledger(P, lambda c: None),
                          lambda: wrapped.callee_ledger(P, lambda c: None),
                          lambda: wrapped.catalogue_with_ledger_read(P, lambda c, l: None)):
            with self.assertRaises(records.JobUnavailable):
                forbidden()
        self.assertEqual(port.calls, before)

    def test_project_start_callback_raises_the_start(self):
        self.stop('project', P)
        self.assertEqual(self.run_pass(), 1)
        self.start('project', P)
        self.write(self.line(1))
        port = DeclaredStorePort(self.f)
        self.assertIsNone(ingest.project_start_callback()(port))
        self.assertEqual(port.calls, [('submit_declared', store_steps.Declared('written', {}))])
        self.assertEqual([row[:2] for row in self.items()],
                         [('project_stopped', P), ('project_started', P)])
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual([r[0] for r in self.items()].count('project_started'), 1)
        self.assertIn('engine_was_down', [r[0] for r in self.items()])

    def test_project_start_callback_after_a_periodic_pass(self):
        self.stop('project', P)
        self.assertEqual(self.run_pass(), 1)
        self.start('project', P)
        self.assertEqual(self.run_pass(), 1)
        port = DeclaredStorePort(self.f)
        before = self.items(), self.cursor('stop_events')
        self.assertIsNone(ingest.project_start_callback()(port))
        self.assertEqual((self.items(), self.cursor('stop_events')), before)
        self.assertEqual([r[0] for r in self.items()].count('project_started'), 1)
        self.write(self.line(1))
        port.before_submit = self.run_pass
        with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
            ingest.startup_callback(self.log)(port)
        self.assertEqual((caught.exception.source, caught.exception.cause),
                         ('lifecycle_log', 'stale'))

    def test_project_start_callback_refused(self):
        self.stop('project', P)
        self.assertEqual(self.run_pass(), 1)
        self.start('project', P)
        port = DeclaredStorePort(self.f)
        port.unavailable = True
        callback = ingest.project_start_callback()
        for start_seen in (False, True):
            with self.subTest(start_seen=start_seen):
                if start_seen:
                    self.assertEqual(self.run_pass(), 1)
                before = self.f.snapshot(), self.cursor('stop_events')
                with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
                    callback(port)
                self.assertEqual((caught.exception.source, caught.exception.cause),
                                 ('stop_events', 'job_unavailable'))
                self.assertEqual((self.f.snapshot(), self.cursor('stop_events')), before)
                self.assertEqual([r[0] for r in self.items()].count('project_started'),
                                 int(start_seen))
        self.assertEqual(self.run_pass(), 0)


class TestStoreOperatorItems(unittest.TestCase):
    REPORT = 'report_awaiting_confirmation'
    START = 'restore_command_started'
    AT = '2026-11-11T06:22:41Z'
    REPORT_PREFIX = ('Projects wait until you confirm you have seen the report of restore job J: '
                     'run adt store confirm-report J. The report is at ')

    def setUp(self):
        self.reset_fixture()

    def reset_fixture(self):
        self.f = QueueFixture(self)
        self.c = self.f.catalogue
        self.port = StorePort(self.f)
        self.log = self.f.root / 'lifecycle.jsonl'

    def write(self, subject, *, kind=REPORT, project=None, fields=None):
        if fields is None:
            fields = {'report_path': f'/recovery/{subject}/report.txt'}
        return store_items.write_raise(self.c, kind=kind, subject=subject, project_id=project,
                                       fields=fields, at_ms=self.f.clock)

    def start(self, subject, *, project=None, scope='the whole store', **fields):
        return self.write(subject, kind=self.START, project=project,
                          fields=dict(dict(command='restore', scope=scope, started_at=self.AT), **fields))

    def end(self, row_id):
        return store_items.write_end(self.c, raise_id=row_id, fields={}, at_ms=self.f.clock)

    def run_pass(self):
        return ingest.store_ingest_pass(self.port, self.log, self.f.clock).raised

    def drain(self):
        return ingest.store_ingest_startup(self.port, self.log, self.f.clock)

    def rows(self, sql, args=()):
        return records._rows(self.c, sql, args)

    def source(self, row_id):
        return self.rows('SELECT item_id FROM queue_store_item_sources WHERE raise_row_id=?',
                         (row_id,))

    def item(self, row_id):
        [(item_id,)] = self.source(row_id)
        item = operations.get_item(self.c, item_id, 20, None)['item']
        item['texts'] = json.loads(item['texts'])
        return item

    def position(self):
        return records.read_cursor(self.c, None, 'store_items').position

    def skips(self):
        return self.rows("SELECT row_ref,reason FROM queue_ingest_skips WHERE source='store_items' "
                         "ORDER BY CAST(row_ref AS INTEGER)")

    def counts(self):
        return tuple(self.rows('SELECT COUNT(*) FROM ' + table)[0][0]
                     for table in ('queue_items', 'queue_conditions', 'queue_events',
                                   'queue_store_item_sources', 'queue_notifications'))

    def test_report_item_raise_and_end(self):
        for mode in ('pending', 'opened', 'restart', 'same-pass', 'no-clear-control'):
            with self.subTest(mode=mode):
                self.reset_fixture()
                row = self.write('J')
                with self.assertRaises(sqlite3.IntegrityError):
                    self.write('J')
                if mode != 'same-pass':
                    self.assertEqual(self.run_pass(), 1)
                    [(item_id,)] = self.source(row)
                    self.assertEqual(self.rows('SELECT condition_key FROM queue_items'), [('"J"',)])
                    self.assertEqual(len(notifier.due_notifications(self.c, self.f.clock, 10)), 1)
                    if mode == 'opened':
                        operations.open_notice(self.c, self.f.operator, item_id, 1, 'open', self.f.clock)
                    if mode == 'restart':
                        self.c.close()
                        self.c = self.f.catalogue = store_layout.open_catalogue(self.f.root / 'store')
                        self.addCleanup(self.c.close)
                        self.port = StorePort(self.f)
                end = self.end(row)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.end(row)
                if mode == 'no-clear-control':
                    with mock.patch.object(records, 'clear_condition', return_value=records.ClearResult('closed')):
                        self.run_pass()
                    self.assertEqual(self.item(row)['state'], 'pending')
                else:
                    self.run_pass()
                    self.assertEqual(self.item(row)['state'], 'dismissed' if mode == 'opened' else 'cleared')
                    self.assertEqual(self.rows('SELECT closed_at_ms FROM queue_conditions'), [(self.f.clock,)])
                bad = self.write('X', fields={})
                last = self.end(bad)
                before = self.counts()
                self.assertEqual(self.run_pass(), 0)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.source(bad), [])
                self.assertEqual(self.position(), last)
                self.assertGreater(last, end)

    def test_creating_route_starting_item_store_scoped(self):
        self.c.execute("UPDATE project_routes SET state='creating' WHERE project_id=?", (P,)).close()
        creating = self.start('J', scope='Acme, which is still being created', command='abandon J')
        inactive = self.start('inactive', project=P, scope='project <project>')
        bad_store = self.start('bad-store', scope='project <project>')
        bad_project = self.start('bad-project', project=Q,
            scope='Acme, which is still being created')
        self.assertEqual(self.run_pass(), 1)
        notice, = notifier.due_notifications(self.c, self.f.clock, 10)
        self.assertEqual(notice.title, 'v3: console_queue')
        prefix = 'A restore command started: abandon J for Acme, which is still being created, run at'
        for value in (self.item(creating)['texts']['sentence'], notice.text):
            self.assertTrue(value.startswith(prefix))
            self.assertNotIn('<project>', value)
        self.assertEqual(self.source(inactive), [])
        self.assertEqual(self.skips(), [(str(bad_store), 'bad_field:scope'), (str(bad_project), 'bad_field:scope')])
        self.assertEqual(self.position(), bad_project)
        self.c.execute("UPDATE project_routes SET state='active' WHERE project_id=?", (P,)).close()
        self.c.execute("DELETE FROM queue_highwaters WHERE source='store_items'").close()
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(inactive)['project_id'], P)

    def test_store_item_missing_field(self):
        a = self.write('A', fields={})
        b = self.write('B', kind=self.START, fields={'command':'restore','scope':'the whole store'})
        c = self.write('C')
        d = self.start('D')
        self.assertEqual(self.run_pass(), 2)
        self.assertEqual(self.position(), d)
        self.assertEqual(self.skips(), [(str(a), 'missing_field:report_path'), (str(b), 'missing_field:started_at')])
        self.assertEqual(self.source(a) + self.source(b), [])
        self.assertEqual(self.item(c)['state'], 'pending')
        self.assertEqual(self.item(d)['state'], 'pending')
        self.assertEqual(self.counts(), (2, 2, 2, 2, 2))

    def test_store_item_keeps_fields(self):
        p = '/recovery/J/report.txt'
        row = self.write('J', fields={'report_path': p})
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.item(row)['fields'], {'report_path': p})
        self.c.execute("DELETE FROM queue_highwaters WHERE source='store_items'").close()
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.item(row)['fields'], {'report_path': p})
        req = records.RaiseRequest('notice', 'seat_registered', P, {'sentence': 'Seat registered'},
                                   {}, {'row_id':'seat-row'}, 'seat-control')
        result = records.raise_item(self.c, records.InternalWriter(), req, self.f.clock,
                                    enabled=frozenset({'pr3'}))
        self.assertIsNone(operations.get_item(self.c, result.item_id, 20, None)['item']['fields'])

    def test_store_item_texts_exact(self):
        first = 'A restore command started: <command> for <scope>, run at <time> after the operator check passed.'
        ordinary = 'It will withdraw go-aheads and approvals there and grants none; what it did follows in its report.'
        abandon = 'It withdraws nothing and grants nothing; <scope> stays held until the step its report names has run.'
        resolve = ("It answers the question about <project>'s unfinished changes and grants nothing: a no undoes them, "
                   "with a notice for each, unless the store was put back, when nothing changes and <project> waits "
                   "for adopt-restored; a yes or not sure starts adopt-restored.")
        for template, byte_count in ((first, 74), (ordinary, 98), (abandon, 93), (resolve, 237)):
            result = template
            for token in ('<command>', '<scope>', '<time>', '<project>'):
                result = result.replace(token, '')
            self.assertEqual(len(result.encode()), byte_count)
        for command, tail in [('restore', ordinary), ('adopt-restored', ordinary), ('new-folder pass', ordinary),
                              ('abandon J', abandon), ('resolve-reservation', resolve)]:
            row = notice_text.StoreItemRow(1, self.START, P, 'J', None, 0,
                                           {'command':command, 'scope':'project <project>', 'started_at':self.AT})
            texts, pins = notice_text.store_item_texts(row)
            self.assertEqual(texts['sentence'],
                             (first + ' ' + tail).replace('<command>', command).replace('<scope>', 'project <project>'))
            self.assertEqual(pins['at'], self.AT)
        row = notice_text.StoreItemRow(2, self.REPORT, None, 'J', None, 0, {'report_path': '/r/J'})
        texts, _ = notice_text.store_item_texts(row)
        self.assertEqual(texts['sentence'], self.REPORT_PREFIX + '/r/J.')
        self.assertEqual(len((self.REPORT_PREFIX + '.').replace('J','').encode()), 123)
        row = notice_text.StoreItemRow(3, self.START, None, 'J', None, 0,
                                       {'command':'mirror recovery','scope':'the whole store','started_at':self.AT})
        self.assertEqual(notice_text.store_item_texts(row), 'bad_field:command')

    def test_store_item_popup_time(self):
        east = timezone(timedelta(hours=1))
        instant_ms = 1794378161000
        shown = '2026-11-11 07:22'
        for value in (self.AT, instant_ms):
            out = notifier.popup_time(value, east)
            self.assertEqual(out, shown)
            self.assertEqual(len(out.encode()), 16)
            self.assertEqual(notifier.popup_time(value, timezone.utc), '2026-11-11 06:22')
        row = self.start('J', project=P, scope='project <project>')
        bad = self.start('K', started_at='2026-11-11 06:22')
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.skips(), [(str(bad), 'bad_field:started_at')])
        self.assertEqual(self.source(bad), [])
        formatter = notifier.popup_time
        with mock.patch.object(notifier, 'popup_time', side_effect=lambda value: formatter(value, east)):
            note, = notifier.due_notifications(self.c, self.f.clock, 10)
        self.assertIn(f'project p, run at {shown} after', note.text)
        self.assertNotIn('<time>', note.text)
        self.assertIn('project <project>, run at <time> after', self.item(row)['texts']['sentence'])

    def test_hold_kind_skipped_until_pr3d(self):
        row = self.write('hold', kind='project_held_5d', project=P, fields={})
        self.end(row)
        last = self.write('J')
        formatter = getattr(notice_text, 'store_item_texts', None)
        if formatter is not None:
            def not_a_skip(item):
                if item.kind == 'project_held_5d':
                    raise ValueError('unknown kind')
                return formatter(item)
            with mock.patch.object(notice_text, 'store_item_texts', side_effect=not_a_skip):
                with self.assertRaises(ValueError):
                    self.run_pass()
            self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
            self.assertEqual(self.position(), 0)
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.position(), last)
        self.assertEqual(self.skips(), [(str(row), 'unknown_kind:project_held_5d')])
        self.assertEqual(self.source(row), [])
        self.assertEqual(self.item(last)['state'], 'pending')

    def test_startup_drains_store_item_backlog(self):
        for mode in ('pass', 'startup', 'unavailable'):
            with self.subTest(mode=mode):
                self.reset_fixture()
                rows = [self.write(str(i), fields={} if i < 100 else None) for i in range(150)]
                if mode == 'pass':
                    self.assertEqual(self.run_pass(), 0)
                elif mode == 'startup':
                    self.assertEqual(self.drain(), 50)
                else:
                    job = ingest.ingest_store_items
                    with mock.patch.object(ingest, 'ingest_store_items', side_effect=[
                            mock.DEFAULT, records.JobUnavailable('test')], wraps=job):
                        with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
                            self.drain()
                    self.assertEqual(caught.exception.source, 'store_items')
                self.assertEqual(self.position(), rows[149 if mode == 'startup' else 99])
                self.assertEqual(len(self.skips()), 100)
                self.assertEqual(self.counts()[0], 50 if mode == 'startup' else 0)

    def test_store_items_cursor(self):
        row = self.write('J', project=P)
        self.assertEqual(self.run_pass(), 1)
        select = "SELECT project_id,position,anchor_id FROM queue_highwaters WHERE source='store_items'"
        self.assertEqual(self.rows(select), [(None, row, None)])
        before = self.source(row)
        records.undo_step(self.c, P, 'undo-J', now_ms=self.f.clock)
        self.assertEqual(self.rows(select), [(None, row, None)])
        self.assertEqual(self.source(row), before)

    def test_store_item_fields_out_of_range_integer(self):
        def write_number(key, value):
            cur = self.c.execute('INSERT INTO store_operator_items (kind,subject,at,fields) VALUES (?,?,?,?)',
                                 (self.REPORT, key, self.f.clock, json.dumps({'report_path':'/r', 'n': value})))
            try:
                return cur.lastrowid
            finally:
                cur.close()
        m = 2 ** 53 - 1
        a, b, c = write_number('A', m), write_number('B', m+1), write_number('C', {'deep': [m+1]})
        d = self.write('D')
        with mock.patch.object(notice_text, 'fit_integers', side_effect=lambda value: value):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.position(), a)
        self.assertEqual(self.source(b) + self.source(c) + self.source(d), [])
        self.assertEqual(self.skips(), [])
        self.assertEqual(self.run_pass(), 3)
        for row, value in ((a,m), (b,str(m+1)), (c,{'deep':[str(m+1)]})):
            self.assertEqual(self.item(row)['fields']['n'], value)
        self.assertEqual(self.item(d)['state'], 'pending')

    def test_store_item_refused_row_rolls_back_and_prefix_commits(self):
        for mode in ('pass', 'startup'):
            with self.subTest(mode=mode):
                self.reset_fixture()
                a, b, c = (self.write(key) for key in 'ABC')
                real = records.raise_item
                def refuse(conn, writer, req, *args, **kwargs):
                    if req.condition_key == 'B':
                        return records.QueueRefusal('E_QUEUE_INVALID', 'subject_unresolved')
                    return real(conn, writer, req, *args, **kwargs)
                with mock.patch.object(records, 'raise_item', side_effect=refuse):
                    if mode == 'startup':
                        with self.assertRaises(ingest.StartupIngestIncomplete) as caught:
                            self.drain()
                        self.assertEqual((caught.exception.source, caught.exception.cause), ('store_items', 'refused'))
                    else:
                        self.assertEqual(self.run_pass(), 1)
                self.assertEqual(self.counts(), (1, 1, 1, 1, 1))
                self.assertEqual(self.position(), a)
                self.assertEqual(self.item(a)['state'], 'pending')
                self.assertEqual(self.source(b) + self.source(c), [])
                self.assertEqual(self.skips(), [])
                self.assertEqual(self.run_pass(), 2)
                self.assertEqual(self.counts(), (3, 3, 3, 3, 3))
                self.assertEqual(self.position(), c)
                self.assertEqual(self.run_pass(), 0)

    def test_store_item_texts_fit(self):
        space = 4096 - len((self.REPORT_PREFIX + '.').encode())
        a = self.write('J', fields={'report_path': 'x' * space})
        b = self.write('K', fields={'report_path': 'x' * (space+1)})
        c = self.write('L', fields={'report_path': 'x\x00'})
        d = self.write('M')
        formatter = notice_text.store_item_texts
        def no_size_check(row):
            if row.id == b:
                return {'sentence': self.REPORT_PREFIX.replace('J', 'K') + 'x' * (space+1) + '.'}, {}
            return formatter(row)
        with mock.patch.object(notice_text, 'store_item_texts', side_effect=no_size_check):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.position(), a)
        self.assertEqual(self.source(b) + self.source(d), [])
        self.assertEqual(self.skips(), [])
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.position(), d)
        self.assertEqual(len(self.item(a)['texts']['sentence'].encode()), 4096)
        self.assertEqual(self.skips(), [(str(b), 'bad_texts:sentence'), (str(c), 'bad_texts:sentence')])
        self.assertEqual(self.source(b) + self.source(c), [])
        self.assertEqual(self.item(d)['state'], 'pending')
