"""Stored admission verdicts and their atomic transaction boundary."""
import base64
import json
from pathlib import Path
import time
from unittest import mock

from relay_engine import daemon, errors, judging, ledger as store, seats
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.rules import Finding
from relay_engine.tests.test_judging import body
from relay_engine.tests.test_ledger import LedgerFixture, WALL


class TestAdmissionJudging(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        self.ledger.execute("INSERT INTO meta(key,value) VALUES('run_id','r')")
        for sender in ('p.planner', 'p.implementer', 'operator', 'r.relay-engine'):
            self.ledger.execute("INSERT INTO seat_events(address,event,occupant_id,key_id) VALUES(?,'occupied','occ','key')", (sender,))
            path = Path(self.root.path, '.engine/seats', sender, 'occ.key')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('tag\n')

    def args(self, raw, sid='candidate', edge=None):
        env = parse_draft(raw.decode())
        return dict(body_b64=base64.b64encode(raw).decode(), envelope=env.headers,
                    tag='tag', submission_id=sid, body_sha256=body_sha256(raw),
                    content_hash=content_hash(env, raw, edge), **({'admits_against': edge} if edge else {}))

    def handler(self, raw, sid='candidate', edge=None):
        return daemon._submit_handler(self.ledger, self.root, self.args(raw, sid, edge))

    def seed(self, raw, path='history.md'):
        env = parse_draft(raw.decode())
        return store.admit(self.ledger, self.root, env, raw, path,
                           origin='hand', stamp='20261013-100000', rendered_path=path)

    def report(self):
        return body('IMPL', 'p.implementer', 'p.planner',
                    extra='PARENT_DISPATCH_ID: grant\nACTIONS_GIT_REF: implemented abcdef1')

    def seq(self, path):
        return self.ledger.execute('SELECT seq FROM relays WHERE rendered_path=?', (path,)).fetchone()[0]

    def test_defective_relay_is_admitted_with_findings(self):
        result = self.handler(self.report())
        self.assertIn('lineage.impl-report-parent', [f['id'] for f in result['findings']])
        self.assertTrue(Path(self.root.path, result['path']).is_file())
        self.seed(body('IMPL', ident='grant', content='DISPATCH IMPL'))
        clean = self.handler(self.report().replace(b'candidate', b'valid-report'), 'valid')
        self.assertEqual(clean['findings'], [])

    def test_result_keys(self):
        result = self.handler(body('SITREP'))
        self.assertEqual(set(result), {'path', 'render_state', 'duplicate', 'advisories', 'findings'})
        self.assertEqual(result['findings'], [])

    def test_collision_allocated_subject_matches_receipt_and_stored_finding(self):
        raw = self.report()
        env = parse_draft(raw.decode())
        expected = store._rendered_path(env, store._format_stamp(store.datetime.fromtimestamp(WALL)))
        occupied = Path(self.root.path, expected)
        occupied.parent.mkdir(parents=True, exist_ok=True)
        occupied.write_text('foreign bytes')
        with mock.patch.object(store.time, 'time', return_value=WALL):
            # Explicit clock is needed because admit's default was bound at import.
            real_admit = store.admit
            def fixed(*args, **kwargs):
                return real_admit(*args, **kwargs, clock=lambda: WALL)
            with mock.patch.object(daemon, 'admit', side_effect=fixed):
                result = self.handler(raw)
        self.assertNotEqual(result['path'], expected)
        self.assertTrue(result['findings'])
        status, stored = store.judgment_for(self.ledger, self.seq(result['path']))
        self.assertEqual(status, 'judged')
        self.assertEqual({f.subject for f in stored}, {result['path']})
        self.assertEqual({f['subject'] for f in result['findings']}, {result['path']})
        self.assertEqual(occupied.read_text(), 'foreign bytes')

    def test_judge_runs_after_allocation_and_before_the_single_insert(self):
        trace = []
        def sql(statement):
            if statement.startswith('INSERT INTO relays('): trace.append('insert')
            if statement.startswith('INSERT INTO admission_judgments('): trace.append('record')
        self.ledger.set_trace_callback(sql)
        def judge(ctx):
            self.assertIsInstance(ctx, store.AdmissionContext)
            self.assertIsNone(ctx.ledger.execute('SELECT seq FROM relays WHERE seq=?', (ctx.seq,)).fetchone())
            self.assertTrue(ctx.rendered_path.endswith('.md'))
            trace.append('judge')
            return 'judged', (Finding('file.lint','error',ctx.rendered_path,(),{},'bad'),)
        def effects(ctx, findings):
            trace.append('effects')
            self.assertEqual(store.judgment_for(ctx.ledger, ctx.seq), ('judged', tuple(findings)))
        result = self.submit(prechecks=[lambda *args: trace.append('prechecks')],
                             advisories=lambda *args: trace.append('legacy') or ['legacy'],
                             judge=judge, effects=effects, clock=lambda: WALL)
        self.assertEqual(trace, ['prechecks','legacy','judge','insert','record','effects'])
        self.assertEqual(json.loads(self.ledger.execute('SELECT advisories_json FROM relays WHERE seq=?', (result.seq,)).fetchone()[0]), ['legacy','file.lint'])
        for point in ('after-judgment', 'after-effects'):
            with self.subTest(point=point):
                def fault(hit):
                    if hit == point: raise store.InjectedFault(point)
                with self.assertRaises(store.InjectedFault):
                    self.submit(submission_id=point, judge=judge, effects=effects, fault=fault)
                self.assertEqual(self.ledger.execute('SELECT count(*) FROM relays').fetchone()[0], 1)
                self.assertEqual(self.ledger.execute('SELECT count(*) FROM admission_judgments').fetchone()[0], 1)

    def test_relay_row_is_inserted_once_with_final_advisories(self):
        trace = []
        self.ledger.set_trace_callback(trace.append)
        result = self.handler(self.report())
        mutations = [s for s in trace if s.lstrip().upper().startswith(('INSERT INTO RELAYS(', 'UPDATE RELAYS', 'DELETE FROM RELAYS'))]
        self.assertEqual(len(mutations), 1)
        self.assertIn('lineage.impl-report-parent', mutations[0])
        self.assertEqual(result['advisories'][-len(result['findings']):], [f['id'] for f in result['findings']])

    def test_replay_returns_stored_findings_without_rejudging(self):
        raw = self.report()
        first = self.handler(raw)
        self.seed(body('IMPL', ident='grant', content='DISPATCH IMPL'))
        with mock.patch.object(judging, 'judge_candidate', side_effect=AssertionError('rejudged')) as judge:
            replay = self.handler(raw)
        judge.assert_not_called()
        self.assertEqual(replay['findings'], first['findings'])
        self.assertEqual(replay['advisories'], first['advisories'])
        self.assertTrue(replay['duplicate'])

    def test_judging_exception_admits_with_failed_status(self):
        raw = body('SITREP')
        with mock.patch.object(judging, 'judge_candidate', side_effect=RuntimeError('injected')):
            first = self.handler(raw)
        self.assertEqual(first['findings'], [])
        self.assertIn('judging.failed', first['advisories'])
        self.assertEqual(store.judgment_for(self.ledger, self.seq(first['path'])), ('failed', ()))
        self.assertEqual(self.handler(raw)['advisories'], first['advisories'])

    def test_fresh_candidate_consuming_invalid_historical_parent_is_flagged(self):
        historical = self.seed(body('SITREP', ident='grant'))
        result = self.handler(self.report())
        self.assertEqual(store.judgment_for(self.ledger, historical.seq), ('none', ()))
        finding = next(f for f in result['findings'] if f['id'] == 'lineage.impl-report-parent')
        self.assertEqual(finding['operands'], ['history.md'])

    def test_engine_boot_relay_is_exempt(self):
        boot = seats.register(self.ledger, self.root, 'new.planner', 'Planner')
        self.assertEqual(store.judgment_for(self.ledger, self.seq(boot['boot_relay'])), ('exempt', ()))

    def test_submitted_relay_is_judged_whatever_its_from(self):
        for i, sender in enumerate(('operator', 'r.relay-engine')):
            raw = body('SITREP', sender, ident='unusual-' + str(i))
            result = self.handler(raw, sender)
            self.assertEqual(store.judgment_for(self.ledger, self.seq(result['path']))[0], 'judged')

    def test_existing_refusals_still_refuse(self):
        raw = body('SITREP')
        args = self.args(raw)
        args['tag'] = 'wrong'
        with self.assertRaises(errors.EngineError) as raised:
            daemon._submit_handler(self.ledger, self.root, args)
        self.assertEqual(raised.exception.code, 'E-KEY-MISMATCH')
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM admission_judgments').fetchone()[0], 0)
        first = self.handler(raw)
        with self.assertRaises(errors.EngineError) as raised:
            self.handler(body('SITREP', 'p.implementer', 'p.planner'), 'collision')
        self.assertEqual(raised.exception.code, 'E-ID-COLLISION')
        self.ledger.execute("INSERT INTO supersession_edges(target_seq,ruling_seq,source,applied) VALUES(?,?,'local',1)", (self.seq(first['path']), self.seq(first['path'])))
        with self.assertRaises(errors.EngineError) as raised:
            self.handler(body('SITREP', content='followup'), 'superseded', first['path'])
        self.assertEqual(raised.exception.code, 'E-SUPERSEDED')
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM admission_judgments').fetchone()[0], 1)
