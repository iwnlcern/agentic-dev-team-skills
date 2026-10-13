"""Designation and declarations are independent of seat occupancy."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from unittest import mock

from relay_engine import cli, daemon, errors, roster, seats
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.tests.test_judging import body
from relay_engine.tests.test_ledger import LedgerFixture


class RosterFixture(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        self.ledger.execute("INSERT INTO meta(key,value) VALUES('run_id','r')")
        self.registration = seats.register(self.ledger, self.root, 'p.planner', 'Planner', top=True)
        self.key = self.registration['key_path']
        self.tag = Path(self.root.path, self.key).read_text().strip()
        roster.designate_top_seat(self.ledger, 'p.planner')
        roster.declare(self.ledger, self.root, [('p.implementer','implementer'),('r.orchestrator-reviewer','orchestrator reviewer')], self.tag)

    def submit_raw(self, raw, sid='submit', tag=None):
        env = parse_draft(raw.decode())
        args = dict(body_b64=base64.b64encode(raw).decode(), envelope=env.headers,
                    tag=self.tag if tag is None else tag, submission_id=sid,
                    body_sha256=body_sha256(raw), content_hash=content_hash(env,raw,None))
        return daemon._submit_handler(self.ledger,self.root,args)

    def command(self, arguments):
        def request(root,op,args,**kwargs):
            daemon._validate_args(op,args)
            return daemon._default_handler(self.ledger,self.root,{}, {}, {'op':op,'args':args})
        out,err=io.StringIO(),io.StringIO()
        with mock.patch.object(cli.client,'request',side_effect=request), redirect_stdout(out),redirect_stderr(err):
            code=cli.main(arguments+['--root',self.root.path])
        return code,out.getvalue(),err.getvalue()


class TestRoster(RosterFixture):
    def test_declare_adds_and_reports_exists(self):
        args=['seat','declare','New.Planner','--role','Planner','--key',self.key]
        first=self.command(args);second=self.command(args)
        self.assertEqual((first[0],second[0]),(0,0))
        self.assertEqual(json.loads(first[1]),{'results':[{'address':'new.planner','role':'planner','result':'added'}]})
        self.assertEqual(json.loads(second[1]),{'results':[{'address':'new.planner','role':'planner','result':'exists'}]})

    def test_declare_on_occupied_seat_leaves_key_working(self):
        before=list(self.ledger.execute('SELECT * FROM seat_events'))
        answer=roster.declare(self.ledger,self.root,[('p.planner','planner')],self.tag)
        self.assertEqual(answer,[{'address':'p.planner','role':'planner','result':'exists'}])
        self.assertEqual(list(self.ledger.execute('SELECT * FROM seat_events')),before)
        self.assertEqual(self.submit_raw(body('SITREP'))['findings'],[])

    def test_declare_without_top_seat_is_no_top_seat_error(self):
        self.ledger.execute("DELETE FROM meta WHERE key='top_seat'")
        code,out,err=self.command(['seat','declare','new.planner','--role','planner','--key',self.key])
        self.assertEqual((code,out),(1,''));self.assertIn('no-top-seat',err);self.assertIn('daemon start --seat',err)

    def test_first_occupant_implementer_is_not_top_seat(self):
        self.ledger.execute("DELETE FROM meta WHERE key='top_seat'")
        seats.register(self.ledger,self.root,'first.implementer','Implementer')
        self.assertIsNone(roster.top_seat(self.ledger))
        with self.assertRaises(errors.EngineError) as error:
            roster.declare(self.ledger,self.root,[('new.planner','planner')],self.tag)
        self.assertEqual(error.exception.code,'no-top-seat')
        self.assertNotIn('routed_notice',self.submit_raw(body('SITREP',target='unknown.planner')))

    def test_daemon_start_with_seat_records_designation_once(self):
        self.ledger.execute("DELETE FROM meta WHERE key='top_seat'")
        for address in ('p.planner','second.planner'):
            seats.startup_top_seat({'ledger':self.ledger,'root':self.root,'top_seat':address,'top_role':'Planner'})
            self.assertEqual(roster.top_seat(self.ledger),'p.planner')

    def test_declare_with_wrong_key_is_key_mismatch(self):
        with self.assertRaises(errors.EngineError) as error:
            roster.declare(self.ledger,self.root,[('new.planner','planner')],'wrong')
        self.assertEqual(error.exception.code,'E-KEY-MISMATCH')
        self.assertNotIn('new.planner',roster.known(self.ledger))

    def test_declare_file_is_all_or_nothing(self):
        path=Path(self.root.path,'names.txt');path.write_text('new.planner Planner\nbad/address Planner\n')
        args=['seat','declare','--from-file',str(path),'--key',self.key]
        code,out,err=self.command(args)
        self.assertEqual((code,out),(1,''));self.assertIn('E-HEADER',err)
        self.assertNotIn('new.planner',roster.known(self.ledger))
        path.write_text('new.planner Planner\nother.reviewer Pair Reviewer\n')
        code,out,err=self.command(args)
        self.assertEqual(code,0,err)
        self.assertEqual([v['address'] for v in json.loads(out)['results']],['new.planner','other.reviewer'])

    def test_known_set_is_roster_plus_any_seat_event_plus_specials(self):
        seats.register(self.ledger,self.root,'old.planner','Planner')
        seats.stand_down(self.ledger,self.root,'old.planner')
        self.assertEqual(roster.known(self.ledger),frozenset({'p.planner','p.implementer','r.orchestrator-reviewer','old.planner','operator','orchestrator'}))
