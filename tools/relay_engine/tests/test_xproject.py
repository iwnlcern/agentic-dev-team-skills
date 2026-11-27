"""Authority's sending-ledger cross-project permission records."""
import ast
from contextlib import closing, contextmanager
from dataclasses import replace
import importlib
from pathlib import Path
import sqlite3
from unittest import mock

from relay_engine import errors, held_relays, queries, restore_cancel, schema, strings
from relay_engine.tests import test_schema


SHA = 'a' * 64
TABLES = ('cross_project_allows', 'cross_project_decisions', 'cross_project_outcomes')
CODE = 'E-AUTHORITY-CROSS-PROJECT-DENIED'


class TestPermissionTables(test_schema.Store):
    def test_tables_strict_owned_and_checked(self):
        conn = self.connect()
        self.relay(conn, 'relay', 1, body_sha=SHA)
        with closing(conn.execute('PRAGMA table_list')) as cursor:
            strict = {row[1]: row[5] for row in cursor}
        for table in TABLES:
            with self.subTest(table=table):
                self.assertEqual(strict.get(table), 1)
                self.assertEqual(schema.TABLE_OWNERS.get(table), 'authority')

        permits = ('q', 1, 10, 'operator', None, None)
        decisions = ('relay', 'q', SHA, 'once', 10, 'operator')
        outcomes = ('key', 'answer', SHA, 'applied', None, 'receipt', 10)
        for table, row, bad in (
            (TABLES[0], permits, ((0, ''), (1, 0), (3, 'seat'),
                                  (4, 11), (5, 'operator'), (5, 'seat'))),
            (TABLES[1], decisions, ((0, 'missing'), (2, '0'), (2, 'A' * 64),
                                    (3, 'always'), (5, 'seat'))),
            (TABLES[2], outcomes, ((0, ''), (0, 'é' * 129), (0, 'k\x00y'), (1, 'unknown'),
                                   (2, 'g' * 64), (3, 'refused'), (3, 'stale'),
                                   (4, 'source_restored'))),
        ):
            sql = 'INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in row) + ')'
            for field, value in bad:
                with self.subTest(table=table, field=field, value=value):
                    invalid = tuple(value if i == field else old for i, old in enumerate(row))
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql, invalid).close()
            conn.execute(sql, row).close()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO cross_project_outcomes VALUES (?,?,?,?,?,?,?)',
                         ('another-key', *outcomes[1:])).close()
        for table in TABLES[1:]:
            for sql in ('UPDATE %s SET rowid = rowid', 'DELETE FROM %s'):
                with self.subTest(table=table, sql=sql):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'insert-only'):
                        conn.execute(sql % table).close()

    def test_denied_code_catalogue(self):
        error = errors.error_for(CODE)
        self.assertEqual(error.cause, 'the operator did not allow this relay to reach the other project')
        self.assertEqual(error.remedy, 'ask the operator, or send it to a seat of your own project')
        self.assertEqual(errors.explain_for(CODE)[0],
                         'a relay to another project waits until the operator answers it, and a deny ends that relay only')
        self.assertEqual(errors.ERRORS[CODE].cls, 'command')
        self.assertIn(CODE, strings._CODE_VALUES)


class TestPermission(test_schema.Store):
    def setUp(self):
        self.x = importlib.import_module('relay_engine.xproject')
        self.present = held_relays.ReceiverFacts('active', False, 'present')
        self.unread = held_relays.ReceiverFacts('active', False, 'not_read')
        self.conn = self.ledger('p')
        self.seq = 0

    def ledger(self, source):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA foreign_keys=ON').close()
        schema.init_v3(conn)
        conn.execute('INSERT INTO projects (id,name,created_at) VALUES (?,?,1)',
                     (source, source)).close()
        conn.commit()
        return conn

    @contextmanager
    def turn(self, conn=None):
        conn = self.conn if conn is None else conn
        conn.execute('BEGIN IMMEDIATE').close()
        try:
            yield conn
            self.assertTrue(conn.in_transaction)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def query(self, sql, args=(), conn=None):
        with closing((self.conn if conn is None else conn).execute(sql, args)) as cursor:
            return cursor.fetchall()

    def new(self, rid='r', targets=('q',), *, state='held', conn=None, sender='p'):
        self.seq += 1
        with self.turn(conn) as ledger:
            self.relay(ledger, rid, self.seq, body_sha=SHA, project=sender, sender='operator')
            for receiver in targets:
                held_relays.admit(ledger, rid, receiver, SHA, (
                    held_relays.Recipient(receiver + '-seat', 'team.implementer@' + receiver, 'to'),
                ), state, self.unread, self.seq)

    def status(self, rid='r', receiver='q'):
        return next(t for t in held_relays.status(self.conn, rid)
                    if t.receiver_project_id == receiver)

    def answers(self, rid='r', choice='once', receiver='q', **overrides):
        data = dict(surface='page', apply_key='answer-' + rid + receiver,
                    expected_body_sha256=SHA, expected_allow_revision=0,
                    expected_restore_job='', facts=self.present)
        data.update(overrides)
        with self.turn():
            return self.x.answer(self.conn, rid, receiver, choice, 'operator', 50, **data)

    def revoke(self, receiver='q', **overrides):
        data = dict(surface='terminal', apply_key='revoke-' + receiver,
                    expected_allow_revision=1, expected_restore_job='', facts=self.present)
        data.update(overrides)
        with self.turn():
            return self.x.revoke_always(self.conn, receiver, 'operator', 60, **data)

    def read_target(self, rid='r', receiver='q'):
        return queries.ForeignReadTarget(rid, SHA, 'p', receiver, receiver + '-seat', 'to')

    def delivered(self):
        return self.query('SELECT relay_id,receiver_project_id,body_sha256 '
                          'FROM xproject_deliveries ORDER BY relay_id,receiver_project_id')

    def counts(self):
        return tuple(self.query('SELECT count(*) FROM ' + table)[0][0] for table in
                     (*TABLES, 'xproject_transfers', 'xproject_deliveries', 'xproject_events'))

    def backup(self, source):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        source.backup(conn)
        conn.execute('PRAGMA foreign_keys=ON').close()
        return conn

    def test_decide_states(self):
        self.new()
        self.assertEqual(self.x.decide(self.conn, 'r', 'q', SHA), 'held')
        self.answers(choice='always', facts=self.unread)
        self.assertEqual(self.x.decide(self.conn, 'r', 'q', SHA), 'allowed')
        self.revoke()
        self.assertEqual(self.x.decide(self.conn, 'r', 'q', SHA), 'held')
        self.answers(expected_allow_revision=2, apply_key='once', facts=self.unread)
        self.assertEqual(self.x.decide(self.conn, 'r', 'q', SHA), 'allowed')
        self.assertEqual(self.x.decide(self.conn, 'r', 'q', 'b' * 64), 'held')
        self.new('deny')
        self.answers('deny', 'deny', expected_allow_revision=2)
        self.assertEqual(self.x.decide(self.conn, 'deny', 'q', SHA), 'denied')

    def test_one_direction(self):
        self.new()
        self.answers(choice='always')
        other = self.ledger('q')
        self.new('back', ('p',), conn=other, sender='q')
        self.assertEqual(self.x.decide(other, 'back', 'p', SHA), 'held')

    def test_answer_pins(self):
        self.new(targets=('q', 'c'))
        self.answers(receiver='c')
        cases = (
            ('source_restored', 'c', dict(expected_restore_job='old', expected_body_sha256='b' * 64)),
            ('relay_not_held', 'c', dict(expected_body_sha256='b' * 64)),
            ('body_changed', 'q', dict(expected_body_sha256='b' * 64, expected_allow_revision=1)),
            ('allow_changed', 'q', dict(expected_allow_revision=1,
                                        facts=held_relays.ReceiverFacts('not_active', False, 'not_read'))),
            ('destination_unavailable', 'q', dict(facts=held_relays.ReceiverFacts('not_active', False, 'not_read'))),
        )
        for pin, receiver, overrides in cases:
            with self.subTest(pin=pin), mock.patch.object(held_relays, 'step') as step:
                before = self.counts()
                outcome = self.answers(receiver=receiver, apply_key=pin, **overrides)
                self.assertEqual((outcome.status, outcome.stale_pin), ('stale', pin))
                self.assertEqual(self.counts(), (*before[:2], before[2] + 1, *before[3:]))
                step.assert_not_called()

    def test_answer_operator_only(self):
        self.new()
        self.answers(choice='always')
        before = self.counts()
        for fn, args, extra in (
            (self.x.answer, ('r','q','always'), dict(expected_body_sha256=SHA)),
            (self.x.revoke_always, ('q',), {}),
        ):
            with self.subTest(call=fn.__name__), self.turn(), self.assertRaises(self.x.ApplyRefused) as refusal:
                fn(self.conn, *args, 'seat', 50, surface='page', apply_key=fn.__name__,
                   expected_allow_revision=1, expected_restore_job='', facts=self.present, **extra)
            self.assertEqual(refusal.exception.code, 'not-operator')
            self.assertEqual(self.counts(), before)

    def test_no_withdraw(self):
        self.assertEqual(set(self.x.__all__), {
            'Decision', 'ApplyOutcome', 'ApplyRefused', 'decide', 'allow_revision',
            'restore_generation', 'release', 'answer', 'revoke_always', 'revoke_all_for_restore',
        })
        self.assertFalse(hasattr(self.x, 'withdraw'))

    def test_release_bound(self):
        self.new()
        self.answers()
        self.new('unreleased')
        target = self.read_target()
        self.assertIs(self.x.release(self.conn, target), True)
        for field, wrong in (('source_project_id','x'), ('destination_project_id','x'),
                             ('relay_id','x'), ('body_sha256','b'*64), ('relay_id','unreleased')):
            with self.subTest(field=field, wrong=wrong):
                self.assertIs(self.x.release(self.conn, replace(target, **{field: wrong})), False)
        fault = mock.Mock()
        fault.execute.side_effect = sqlite3.OperationalError('unreadable ledger')
        with self.assertRaisesRegex(sqlite3.OperationalError, 'unreadable ledger'):
            self.x.release(fault, target)

    def test_permission_grants_nothing_else(self):
        from relay_engine import claims, submit
        from relay_engine.tests.test_slips import QIMPL, QREVIEWER, SlipStore, with_repo
        from relay_engine.tests.test_submit import IMPL, PLANNER, draft

        fixture = SlipStore()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        go = fixture.submit(draft())
        digest = fixture.conn.execute('SELECT body_sha256 FROM relays WHERE id=?',
                                      (go.relay_id,)).fetchone()[0]
        with self.turn(fixture.conn):
            held_relays.admit(fixture.conn, go.relay_id, 'q', digest, (), 'held', self.unread, 10)
            self.x.answer(fixture.conn, go.relay_id, 'q', 'always', 'operator', 11,
                          surface='page', apply_key='allow', expected_body_sha256=digest,
                          expected_allow_revision=0, expected_restore_job='', facts=self.present)
        foreign_go = fixture.submit(with_repo(draft(kind='IMPL', needs='act',
                                   parent=go.relay_id, to=QIMPL.address)))
        self.assertIsNone(fixture.slip(foreign_go))
        self.assertEqual(fixture.conn.execute('SELECT count(*) FROM slips').fetchone(), (0,))
        self.assertEqual(fixture.conn.execute('SELECT count(*) FROM branch_bindings').fetchone(), (0,))
        with self.assertRaisesRegex(ValueError, 'same project'):
            claims.assign_reviewer(fixture.conn, PLANNER.team_instance_id,
                                   QREVIEWER.seat_id, 'operator', 12)
        fixture.conn.execute("INSERT INTO repositories (id,project_id,name) VALUES ('qrepo','q','qrepo')").close()
        fixture.conn.commit()
        answer = fixture.act(repo='qrepo')
        self.assertEqual((answer.outcome, answer.code), ('deny', 'authority.slip-missing'))
        self.assertNotEqual(fixture.act(repo='qrepo', kind='pr-merge').outcome, 'allow')

        # The live check above has no own IMPL to diagnose. Exercise the pinned
        # diagnostic separately with a test-only legacy-shaped, unissued row.
        operator = submit.SeatRef('operator', 'operator', 'operator', None, 'p')
        ctx = replace(fixture.ctx(operator), after_admit=None, after_admit_writes=frozenset(),
                      after_admit_reads_denied=frozenset())
        own = submit.submit(fixture.conn, ctx, with_repo(draft(
            kind='IMPL', needs='act', to=IMPL.address)))
        fixture.conn.execute("UPDATE relays SET repository_id='qrepo' WHERE id=?",
                             (own.relay_id,)).close()
        fixture.conn.commit()
        diagnostic = fixture.act(repo='qrepo')
        self.assertEqual((diagnostic.outcome, diagnostic.code), ('deny', 'authority.slip-missing'))
        self.assertIn('outside this project', diagnostic.reason)

    def allowed_group(self):
        self.new('a')
        self.answers('a', 'always', facts=self.unread)
        self.new('b', state='allowed')

    def test_revoke_keeps_delivered(self):
        self.allowed_group()
        with self.turn():
            held_relays.step(self.conn, 'a', 'q', 'allowed', self.present, 51)
        self.revoke()
        self.assertIs(self.x.release(self.conn, self.read_target('a')), True)
        self.assertEqual(self.status('b').state, 'held')

    def test_destinations_disjoint(self):
        self.new(targets=('q', 'c'))
        self.assertEqual([self.status(receiver=r).state for r in ('q','c')], ['held','held'])
        self.answers(receiver='q')
        delivered = [('r','q',SHA)]
        self.assertEqual(self.delivered(), delivered)
        self.assertEqual((self.status(receiver='q').state,self.status(receiver='c').state),
                         ('delivered','held'))
        self.answers(choice='deny', receiver='c')
        self.assertEqual(self.status(receiver='c').state, 'denied')
        self.assertEqual(self.delivered(), delivered)
        self.assertIs(self.x.release(self.conn, self.read_target()), True)

    def test_revoke_orders_with_transfer(self):
        self.allowed_group()
        with self.turn():
            held_relays.step(self.conn, 'b', 'q', self.x.decide(self.conn, 'b', 'q', SHA), self.present, 51)
        self.revoke()
        with self.turn():
            decision = self.x.decide(self.conn, 'a', 'q', SHA)
            self.assertEqual(decision, 'held')
            held_relays.step(self.conn, 'a', 'q', decision, self.present, 61)
        self.assertEqual(self.status('a').state, 'held')
        self.assertEqual(self.delivered(), [('b','q',SHA)])
        self.assertIs(self.x.release(self.conn, self.read_target('b')), True)

    def test_revoke_returns_undelivered_to_held(self):
        self.allowed_group()
        self.revoke()
        self.assertEqual([self.status(r).state for r in ('a','b')], ['held','held'])
        for rid in ('a','b'):
            with self.turn():
                held_relays.step(self.conn, rid, 'q', self.x.decide(self.conn, rid, 'q', SHA),
                                 self.present, 61)
        self.assertEqual(self.delivered(), [])
        asks = held_relays.desired_asks(self.conn, {'q': 2}, '')
        self.assertEqual([ask.key for ask in asks], [('a', 'q'), ('b', 'q')])

    def test_answer_steps_in_one_commit(self):
        for rid, choice, facts, state in (('delivery','once',self.present,'delivered'),
                                          ('queued','once',self.unread,'allowed'),
                                          ('deny','deny',self.present,'denied')):
            self.new(rid)
            with self.turn():
                self.x.answer(self.conn, rid, 'q', choice, 'operator', 50,
                              surface='page', apply_key=rid, expected_body_sha256=SHA,
                              expected_allow_revision=0, expected_restore_job='', facts=facts)
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.status(rid).state, state)
                self.assertEqual(self.query('SELECT choice FROM cross_project_decisions WHERE relay_id=?',
                                            (rid,)), [(choice,)])
                self.assertEqual(self.delivered(), [('delivery','q',SHA)])

    def test_revoke_steps_lost_relays(self):
        for active in (True, False):
            with self.subTest(active=active):
                self.conn = self.ledger('p')
                self.allowed_group()
                before = self.query('SELECT count(*) FROM xproject_events')[0][0]
                facts = self.present if active else held_relays.ReceiverFacts('not_active',False,'not_read')
                self.revoke(facts=facts)
                want = ('held',None) if active else ('returned','destination_not_active')
                self.assertEqual([(self.status(r).state,self.status(r).reason) for r in ('a','b')], [want,want])
                self.assertEqual(self.query('SELECT count(*) FROM xproject_events'), [(before+2,)])
                self.assertEqual(self.delivered(), [])

    def once_group(self):
        self.allowed_group()
        self.new('c')
        self.answers('c', facts=self.unread, expected_allow_revision=1)

    def test_once_survives_revoke(self):
        self.once_group()
        before = self.status('c')
        self.revoke()
        self.assertEqual(self.status('c'), before)
        self.assertEqual(self.x.decide(self.conn, 'c', 'q', SHA), 'allowed')

    def test_revoke_leaves_once_relay(self):
        self.once_group()
        before = self.status('c')
        events = self.query("SELECT * FROM xproject_events WHERE relay_id='c'")
        with mock.patch.object(held_relays, 'step', wraps=held_relays.step) as step:
            self.revoke()
        self.assertEqual([call.args[1] for call in step.call_args_list], ['a','b'])
        self.assertTrue(all(call.args[4].seats=='not_read' for call in step.call_args_list))
        self.assertEqual([self.status(r).state for r in ('a','b')], ['held','held'])
        self.assertEqual((self.status('c'), self.query("SELECT * FROM xproject_events WHERE relay_id='c'")),
                         (before,events))
        self.assertEqual(self.delivered(), [])
        with self.turn():
            held_relays.step(self.conn, 'c', 'q', self.x.decide(self.conn, 'c', 'q', SHA), self.present, 61)
        self.assertEqual(self.delivered(), [('c','q',SHA)])

    def test_allow_revision(self):
        self.new()
        before = self.conn.total_changes
        self.assertEqual(self.x.allow_revision(self.conn, 'q'), 0)
        self.assertEqual(self.conn.total_changes, before)
        self.answers(choice='always')
        self.assertEqual(self.x.allow_revision(self.conn, 'q'), 1)
        self.revoke()
        before = self.conn.total_changes
        self.assertEqual(self.x.allow_revision(self.conn, 'q'), 2)
        self.assertEqual(self.conn.total_changes, before)

    def test_answer_needs_the_turn(self):
        self.new()
        options = dict(surface='page', expected_allow_revision=0, expected_restore_job='',
                       facts=self.unread)
        before = self.counts()
        for call,args,extra in (
            (self.x.answer, ('r','q','once'), dict(expected_body_sha256=SHA)),
            (self.x.revoke_always, ('q',), {}),
        ):
            with self.assertRaises(ValueError):
                call(self.conn, *args, 'operator', 50, apply_key=call.__name__, **options, **extra)
            self.assertEqual(self.counts(), before)
        with self.assertRaisesRegex(RuntimeError, 'after decision'), self.turn():
            with mock.patch.object(held_relays, 'step', side_effect=RuntimeError('after decision')):
                self.x.answer(self.conn, 'r','q','once','operator',50, apply_key='failure',
                              expected_body_sha256=SHA, **options)
        self.assertEqual(self.counts(), before)
        self.answers(choice='always', facts=self.unread)
        self.revoke()

    def test_answer_repeat_returns_first_outcome(self):
        self.new()
        first = self.answers(facts=self.unread)
        snapshot = self.counts()
        changes = self.conn.total_changes
        with mock.patch.object(held_relays,'step') as step:
            self.assertEqual(self.answers(), first)
            step.assert_not_called()
        self.assertEqual((self.counts(),self.conn.total_changes), (snapshot,changes))
        with self.assertRaises(ValueError):
            self.answers(choice='deny')
        stale = self.answers(apply_key='stale', expected_body_sha256='b'*64)
        self.assertEqual(stale.status, 'stale')
        self.assertEqual(self.answers(apply_key='stale', expected_body_sha256='b'*64), stale)
        self.new('refused')
        with self.assertRaises(self.x.ApplyRefused) as refused:
            self.answers('refused', surface='other')
        self.assertEqual(refused.exception.code, 'surface-not-allowed')
        self.assertEqual(self.query("SELECT * FROM cross_project_outcomes WHERE apply_key='answer-refusedq'"), [])
        self.assertEqual(self.answers('refused').status, 'applied')

    def test_answer_after_restore_is_stale(self):
        self.new()
        snapshot = self.backup(self.conn)
        self.assertEqual(self.x.restore_generation(self.conn,'p'), '')
        self.assertEqual(self.answers(choice='always', facts=self.unread).status, 'applied')
        self.assertEqual(self.x.allow_revision(self.conn,'q'), 1)
        self.conn = self.backup(snapshot)
        restore_cancel.cancel_all(self.conn,'p','J1',90)
        self.assertEqual(self.x.restore_generation(self.conn,'p'), 'J1')
        with mock.patch.object(held_relays,'step') as step:
            rejected = self.answers(choice='always')
            step.assert_not_called()
        self.assertEqual((rejected.status,rejected.stale_pin), ('stale','source_restored'))
        self.assertEqual(self.counts()[:2], (0,0))
        self.assertEqual(self.status().state, 'held')
        self.new('new')
        self.assertEqual(self.x.decide(self.conn,'new','q',SHA), 'held')
        self.assertEqual(self.answers(choice='always', apply_key='new-choice', expected_restore_job='J1',
                                      facts=self.unread).status, 'applied')
        self.conn = self.backup(snapshot)
        restore_cancel.cancel_all(self.conn,'p','J2',91)
        late = self.answers(choice='always', apply_key='new-choice', expected_restore_job='J1',
                            facts=self.unread)
        self.assertEqual((late.status,late.stale_pin), ('stale','source_restored'))

    def test_restored_receipt_is_historical(self):
        self.new()
        first = self.answers(choice='always', facts=self.unread)
        self.conn = self.backup(self.conn)
        restore_cancel.cancel_all(self.conn,'p','J1',90)
        before = self.conn.total_changes
        with mock.patch.object(held_relays,'step') as step, mock.patch.object(self.x,'decide') as decide:
            self.assertEqual(self.answers(choice='always'), first)
            step.assert_not_called()
            decide.assert_not_called()
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(self.x.allow_revision(self.conn,'q'), 2)
        self.assertEqual(self.x.decide(self.conn,'r','q',SHA), 'held')
        with self.assertRaises(ValueError):
            self.answers(choice='once')

    def test_imports_one_way(self):
        engine = Path(self.x.__file__).parent
        for file, name, expected in (('xproject.py','restore_cancel',False),
                                     ('restore_cancel.py','xproject',True)):
            tree = ast.parse((engine / file).read_text())
            names = {a.name.rsplit('.',1)[-1] for n in ast.walk(tree)
                     if isinstance(n,(ast.Import,ast.ImportFrom)) for a in n.names}
            self.assertEqual(name in names, expected)
