"""Cross-project relay admission through the real StoreWriter."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from relay_engine import api_submit, api_writer, errors, schema, store_layout, submit
from relay_engine.tests import test_api_submit as fixtures
from relay_engine.tests.test_api_writer import Job
from relay_engine.tests.test_submit import draft
from relay_engine.tests.writer_support import serve_for_test


P = fixtures.P
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
R = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
REMOTE = 'remote.implementer@receiver'
TABLES = ('relays', 'recipients', 'xproject_transfers', 'xproject_recipients',
          'xproject_events', 'xproject_deliveries', 'refusals')


class CrossProjectAdmission(unittest.TestCase):
    setUp = fixtures.SubmitServiceTests.setUp
    call = fixtures.SubmitServiceTests.call

    def seed(self, project=Q, name='receiver', *, seat=True, served=True, creating=False):
        with closing(store_layout.open_ledger(self.root, project, create=True)) as conn:
            conn.execute('INSERT INTO projects(id,name,created_at) VALUES (?,?,1)', (project, name)).close()
            conn.execute("INSERT INTO team_instances (id,project_id,name,level,opened_at,opened_by)"
                         " VALUES ('remote-team',?,'remote','pair',1,'operator')", (project,)).close()
            if seat:
                conn.execute("INSERT INTO seats(id,team_instance_id,role,state)"
                             " VALUES (?,'remote-team','implementer','occupied')", (project,)).close()
            conn.commit()
        with closing(store_layout.open_catalogue(self.root)) as conn:
            row = conn.execute('SELECT state FROM project_routes WHERE project_id=?', (project,)).fetchone()
            state = 'creating' if creating else 'active'
            if row is None:
                conn.execute('INSERT INTO project_routes(project_id,name,state,created_at)'
                             ' VALUES (?,?,?,1)', (project, name, state)).close()
            conn.commit()
        if served:
            serve_for_test(self.writer, project)

    def send(self, to=REMOTE, key='xp', **kw):
        return self.call(draft(to=to, **kw), key=key).response

    def durable(self, sql, params=()):
        path = store_layout.ledger_path(self.root, P)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn, \
                closing(conn.execute(sql, params)) as cursor:
            return cursor.fetchall()

    def counts(self):
        return tuple(self.durable('SELECT count(*) FROM ' + t)[0][0] for t in TABLES)

    def refused(self, result, kind, *, engine=None):
        self.assertIn('error', result, result)
        cause = result['error']['cause']
        self.assertEqual(cause, {'kind': kind} if engine is None
                         else {'kind': kind, 'engine_code': engine})

    def admitted(self, result):
        self.assertIn('result', result, result)
        return result['result']['relay_id']

    def write(self, callback):
        def run(conn):
            callback(conn)
            return api_writer.JobOutcome(True, None)
        self.assertTrue(self.writer.submit(P, Job(run, frozenset({self.writer.ledger_file(P)}))).commit)

    @contextmanager
    def intercept(self, callback=lambda _turn, value: value):
        real = self.writer.submit_transfer
        outcomes = []

        def forward(job):
            def run(turn):
                self.assertIsInstance(turn.sender, api_writer.TurnConnection)
                return callback(turn, job.run(turn))
            outcome = real(replace(job, run=run))
            outcomes.append(outcome)
            return outcome

        with mock.patch.object(self.writer, 'submit_transfer', side_effect=forward):
            yield outcomes

    def trace_sender(self):
        trace = []
        self.write(lambda conn: conn.set_trace_callback(trace.append))
        trace.clear()
        return trace

    def test_local_draft_unchanged(self):
        with self.intercept() as outcomes:
            relay = self.admitted(self.send(to='platform.implementer'))
        self.assertEqual(outcomes, [])
        self.assertEqual(self.durable('SELECT relay_id,seat_id FROM recipients'),
                         [(relay, 'implementer')])

    def test_held_admission(self):
        self.seed()
        relay = self.admitted(self.send(to='platform.implementer, ' + REMOTE))
        self.assertEqual(self.durable('SELECT relay_id,state FROM xproject_transfers'), [(relay, 'held')])
        self.assertEqual(self.durable('SELECT relay_id,seat_id FROM recipients'), [(relay, 'implementer')])
        self.assertEqual(self.durable('SELECT relay_id,seat_id FROM xproject_recipients'), [(relay, Q)])

    def test_unknown_or_creating_project(self):
        self.seed(served=False, creating=True)
        for name in ('missing', 'receiver'):
            with self.subTest(name=name), self.intercept() as outcomes, mock.patch.object(
                    self.writer, 'read_catalogue', wraps=self.writer.read_catalogue) as classify:
                self.refused(self.send(to='remote.implementer@' + name, key=name),
                             'integrity', engine='E-ADDRESS')
                self.assertEqual(outcomes, [])
                self.assertEqual(classify.call_count, 1)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 2))

    def test_absent_seat_refused_recorded(self):
        self.seed(seat=False)
        with self.intercept() as outcomes:
            self.refused(self.send(), 'integrity', engine='E-ADDRESS')
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].commit)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 1))

    def test_unreadable_receiver_not_stored(self):
        self.seed(served=False)
        self.refused(self.send(), 'admission_not_stored')
        self.assertEqual(self.counts(), (0,) * len(TABLES))

    def test_route_changed_not_stored(self):
        self.seed()
        real = self.writer.submit_transfer

        def withdraw_route(job):
            with closing(store_layout.open_catalogue(self.root)) as conn:
                conn.execute("UPDATE project_routes SET name='moved' WHERE project_id=?", (Q,)).close()
                conn.commit()
            return real(job)

        with mock.patch.object(self.writer, 'submit_transfer', side_effect=withdraw_route):
            self.refused(self.send(), 'admission_not_stored')
        self.assertEqual(self.counts(), (0,) * len(TABLES))

    def test_two_receivers_all_or_nothing(self):
        self.seed()
        self.seed(R, 'other', seat=False)
        with self.intercept() as outcomes:
            self.refused(self.send(to=REMOTE + ', remote.implementer@other'),
                         'integrity', engine='E-ADDRESS')
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].commit)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 1))

    def test_replay_with_receiver_unreadable(self):
        self.seed()
        first = self.admitted(self.send())
        self.writer.request_project_stop(Q, 'fixture')
        with self.intercept() as outcomes, mock.patch.object(
                api_writer.TransferTurn, 'read_receiver', side_effect=AssertionError('read on replay')):
            self.assertEqual(self.admitted(self.send()), first)
        self.assertEqual(outcomes, [])

    def test_allowed_delivers_in_commit(self):
        self.seed()
        self.write(lambda conn: conn.execute(
            "INSERT INTO cross_project_allows(receiver_project_id,revision,granted_at,granted_by)"
            " VALUES (?,1,1,'operator')", (Q,)).close())
        relay = self.admitted(self.send())
        self.assertEqual(self.durable('SELECT state FROM xproject_transfers'), [('delivered',)])
        self.assertEqual(self.durable('SELECT relay_id FROM xproject_deliveries'), [(relay,)])
        self.assertEqual(self.durable('SELECT to_state FROM xproject_events'), [('delivered',)])

    def test_no_outer_transaction_control(self):
        self.seed()
        trace = self.trace_sender()
        self.admitted(self.send())
        controls = [s for s in trace if s in ('BEGIN IMMEDIATE', 'BEGIN', 'COMMIT', 'END', 'ROLLBACK')]
        self.assertEqual(controls[-2:], ['BEGIN IMMEDIATE', 'COMMIT'])
        self.assertEqual(controls.count('COMMIT'), 1)
        self.assertIn('SAVEPOINT engine_transfer_admission', trace)
        self.assertIn('RELEASE engine_transfer_admission', trace)

    def test_hook_failure_rollback_answer(self):
        self.seed()
        self.write(lambda conn: conn.execute('CREATE TABLE rollback_witness (label TEXT)').close())
        self.enterContext(mock.patch.dict(schema.TABLE_OWNERS, {'rollback_witness': 'authority'}))
        real = api_submit.ServiceContext.submit_context

        def fail(_conn, _admitted, _draft, _ctx):
            raise ValueError('test hook failure')

        for label in ('already-voided', 'voided', None):
            def repair(conn, _ctx):
                conn.execute('INSERT INTO rollback_witness VALUES (?)', (label,)).close()
                return label

            def configure(service, *args, **kwargs):
                return replace(real(service, *args, **kwargs), after_admit=fail, after_rollback=repair,
                               after_admit_writes=frozenset({'rollback_witness'}))

            with self.subTest(label=label), self.intercept() as outcomes, mock.patch.object(
                    api_submit.ServiceContext, 'submit_context', configure):
                remote = self.send(key='remote-' + str(label))['error']
                local = self.send(to='platform.implementer', key='local-' + str(label))['error']
                self.assertEqual(local['cause'], {'kind': 'authority_unavailable'})
                self.assertEqual(remote, local)
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(outcomes[0].commit)
                err = outcomes[0].value.error
                self.assertEqual(err.code, 'E-ADMISSION-HOOK')
                if label == 'already-voided':
                    self.assertEqual(err.reservation, 'undone-with-notice')
                    self.assertEqual(err.as_dict()['reservation_note'],
                        'what the admission hook ' + submit._hook_name(fail)
                        + ' reserved was undone by another writer, and the operator was told')
                else:
                    self.assertIsNone(err.reservation)
                    self.assertEqual(set(err.as_dict()), {'code', 'cls', 'cause', 'remedy'})
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 6))
        self.assertEqual(self.durable('SELECT label FROM rollback_witness'),
                         [(x,) for x in ('already-voided', 'already-voided', 'voided', 'voided', None, None)])

    def test_committed_outcome_survives_cleanup_failure(self):
        for admit in (True, False):
            with self.subTest(admit=admit):
                if not admit:
                    # A reported sender cleanup failure retires its connection.
                    self.doCleanups()
                    self.setUp()
                self.seed(seat=admit)
                real_cleanup = self.writer._cleanup_transfer_connections
                injected = []

                def cleanup_with_failure(conns, *args, **kwargs):
                    failures = real_cleanup(conns, *args, **kwargs)
                    self.assertEqual(failures, {})
                    if P in conns:
                        injected.append(P)
                        return {P: sqlite3.OperationalError('observed cleanup failure')}
                    return failures

                if not admit:
                    local = self.send(to='absent.planner', key='local-refusal')
                    self.refused(local, 'integrity', engine='E-ADDRESS')
                with self.intercept() as outcomes, mock.patch.object(
                        self.writer, '_cleanup_transfer_connections', side_effect=cleanup_with_failure):
                    response = self.send(key='cleanup-outcome')
                self.assertEqual(injected, [P])
                self.assertEqual(len(outcomes), 1)
                outcome, = outcomes
                self.assertTrue(outcome.commit)
                self.assertFalse(outcome.indeterminate)
                self.assertEqual(outcome.fault, 'rollback_failed')
                if admit:
                    self.assertIsInstance(outcome.value, submit.Admitted)
                    self.assertEqual(self.durable('SELECT id FROM relays'), [(outcome.value.relay_id,)])
                    self.assertEqual(self.admitted(response), outcome.value.relay_id)
                else:
                    self.assertIsInstance(outcome.value, submit.TurnRefusal)
                    self.assertEqual(self.counts()[0], 0)
                    self.assertEqual(self.counts()[-1], 2)
                    self.assertEqual(response['error'], local['error'])

    def test_writer_outcome_map(self):
        self.seed()

        def deferred_fk(conn):
            conn.execute('CREATE TABLE fk_parent (id INTEGER PRIMARY KEY)').close()
            conn.execute('CREATE TABLE fk_child (pid INTEGER REFERENCES fk_parent(id)'
                         ' DEFERRABLE INITIALLY DEFERRED)').close()

        self.write(deferred_fk)

        def orphan(turn, outcome):
            self.assertTrue(outcome.commit)
            self.assertIsInstance(outcome.value, submit.Admitted)
            turn.sender.execute('INSERT INTO fk_child VALUES (1)').close()
            return outcome

        real_commit = api_writer._commit_sender

        def committed_then_raise(conn):
            real_commit(conn)
            raise sqlite3.IntegrityError('fixture after commit')

        with self.subTest(fault='commit_failed'), self.intercept(orphan) as outcomes:
            self.refused(self.send(), 'admission_not_stored')
            self.assertEqual(outcomes[0].fault, 'commit_failed')
            self.assertEqual(outcomes[0].value.sqlite_errorname, 'SQLITE_CONSTRAINT_FOREIGNKEY')
            self.assertEqual(self.counts(), (0,) * len(TABLES))
            self.assertEqual(self.durable('SELECT * FROM fk_child'), [])

        with self.subTest(fault='commit_unknown'), self.intercept() as outcomes, mock.patch.object(
                api_writer, '_commit_sender', committed_then_raise):
            self.refused(self.send(key='after'), 'admission_unknown')
            self.assertEqual(outcomes[0].fault, 'commit_unknown')
            self.assertIsInstance(outcomes[0].value, sqlite3.IntegrityError)
            self.assertEqual(self.durable("SELECT submission_key FROM relays"), [('after',)])

        for committing in (False, True):
            with self.subTest(committing=committing):
                self.setUp()
                self.seed()
                self.stopped(committing)

    def stopped(self, committing):
        parked, release, interrupted = (threading.Event() for _ in range(3))
        phase = 'commit_permitted' if committing else 'finishing'
        real_interrupt = self.writer._interrupt

        def pause(site):
            if site == phase and self.writer._active_request.kind == 'transfer':
                parked.set()
                self.assertTrue(release.wait(4))

        def interrupt(conns):
            real_interrupt(conns)
            interrupted.set()

        epoch = SimpleNamespace(attempt=None, admission_closed=False)
        with self.intercept() as outcomes, mock.patch.object(self.writer, '_epoch', epoch), \
                mock.patch.object(self.writer, '_phase_hook', pause), \
                mock.patch.object(self.writer, '_interrupt', interrupt), ThreadPoolExecutor(2) as pool:
            pending = pool.submit(self.send, key=f'stop-{committing}')
            try:
                self.assertTrue(parked.wait(2), 'actual transfer did not reach stop seam')
                now = time.monotonic()
                epoch.attempt = SimpleNamespace(active=now + (0.2 if committing else 0), work=now + 3)
                epoch.admission_closed = True
                fence = pool.submit(self.writer.fence_for_stop, epoch.attempt)
                self.assertTrue(interrupted.wait(2))
                if not committing:
                    release.set()
                self.refused(pending.result(3), 'admission_unknown' if committing
                             else 'admission_not_stored')
                self.assertEqual(outcomes[0].fault, 'stop_commit_unknown' if committing else 'stop_unwritten')
            finally:
                release.set()
            fence.result(3)
        if not committing:
            self.assertEqual(self.durable("SELECT id FROM relays WHERE submission_key='stop-False'"), [])

    def test_in_lock_replay_hit(self):
        self.seed()
        real = self.writer.submit_transfer
        earlier = []

        def insert_first(job):
            earlier.append(real(job).value)
            return real(job)

        trace = self.trace_sender()
        with mock.patch.object(self.writer, 'submit_transfer', insert_first):
            relay = self.admitted(self.send())
        self.assertEqual(earlier[0].relay_id, relay)
        self.assertEqual(self.durable('SELECT id FROM relays'), [(relay,)])
        self.assertEqual(trace.count('SAVEPOINT engine_transfer_admission'), 2)
        self.assertEqual(trace.count('RELEASE engine_transfer_admission'), 2)

    def test_refusal_order_declared_first(self):
        self.seed(seat=False)
        self.seed(R, 'other', seat=False)
        with self.intercept() as outcomes:
            self.refused(self.send(to='remote.implementer@other', cc=REMOTE), 'integrity', engine='E-ADDRESS')
        self.assertEqual(len(outcomes), 1, 'refusal did not enter a transfer turn')
        self.assertEqual(outcomes[0].value.error.params['field'], 'CC')

    def test_turn_guards_keep_writer_policy(self):
        self.seed()
        real = api_submit.ServiceContext.submit_context
        seen = []
        checked_on = []

        def hook(conn, _admitted, _draft, ctx):
            self.assertIsInstance(conn, api_writer.TurnConnection)
            seen.append(submit.file_system_relay(conn, ctx, draft(to='platform.implementer',
                                                                bundle='nested', subject='nested')))

        def configure(service, *args, **kwargs):
            ctx = real(service, *args, **kwargs)

            def check(conn):
                ctx.sender_check(conn)
                checked_on.append(isinstance(conn, api_writer.TurnConnection))
                if isinstance(conn, api_writer.TurnConnection):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, 'not authorized'):
                        conn.execute('INSERT INTO guard_witness VALUES (0)').close()

            return replace(ctx, sender_check=check, after_admit=hook)

        def probe(turn, outcome):
            self.assertTrue(outcome.commit)
            self.assertNotIn(turn.sender, submit._CHECK_FRAMES)
            self.assertNotIn(turn.sender, submit._HOOK_FRAMES)
            turn.sender.execute('INSERT INTO guard_witness VALUES (1)').close()
            return outcome

        self.write(lambda conn: conn.execute('CREATE TABLE guard_witness (n INTEGER)').close())
        with mock.patch.object(api_submit.ServiceContext, 'submit_context', configure), self.intercept(probe):
            self.admitted(self.send())
        self.assertEqual(self.durable('SELECT id FROM relays WHERE sender_special=\'system\''), [(seen[0].relay_id,)])
        self.assertEqual(checked_on, [False, True])
        self.assertEqual(self.durable('SELECT n FROM guard_witness'), [(1,)])

    def test_foreign_reply_refused_before_replies(self):
        self.seed()
        with self.intercept() as outcomes:
            self.refused(self.send(to='platform.implementer', parent='#1@receiver'),
                         'integrity', engine='E-PARENT-UNKNOWN')
        self.assertEqual(len(outcomes), 1, 'foreign parent did not enter a transfer turn')
        error = outcomes[0].value.error
        self.assertEqual(error.cause, errors.error_for(
            'E-PARENT-UNKNOWN', variant='foreign', **error.params).cause)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 1))
