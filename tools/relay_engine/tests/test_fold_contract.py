"""Receipt and managed-audit regressions from the fold review."""
import unittest
from unittest import mock
from relay_engine import judging, strings
from relay_engine.tests import test_admission_judging as admission
from relay_engine.tests import test_history_audit as history


class TestFoldReceipt(admission.TestAdmissionJudging):
    def test_M9_failed_receipt_is_not_clean_and_replay_matches(self):
        raw=admission.body('SITREP')
        with mock.patch.object(judging,'judge_candidate',side_effect=RuntimeError('injected')):
            result=self.handler(raw)
        expected=[{'id':'judging.failed','lint_severity':'warn','subject':result['path'],
                   'operands':[],'params':{},'message':strings.render('judging.failed')}]
        self.assertEqual(result['findings'],expected)
        with mock.patch.object(judging,'judge_candidate',side_effect=AssertionError('rejudged')):
            replay=self.handler(raw)
        self.assertEqual(replay['findings'],expected)


class TestFoldAudit(history.TestHistoryAudit):
    def test_M8_record_known_file_defect_is_not_audit_history(self):
        strict=history.rules.lint_relay_root(self.root)
        self.assertTrue(any('HUMAN_GATE_REQUIRED' in e for e in strict.errors))
        for audit in (False,True):
            result=self.lint(history_audit=audit)
            self.assertFalse(any('HUMAN_GATE_REQUIRED' in e for e in result.errors+result.warnings))

class TestFoldCache(admission.TestAdmissionJudging):
    def test_M12_daemon_fingerprint_reused_by_submits_and_notices(self):
        from relay_engine import daemon, version
        self.ledger.execute("INSERT INTO meta(key,value) VALUES('top_seat','p.planner')")
        state={}
        with mock.patch.object(version,'fingerprint',return_value='a'*64) as fingerprint:
            for name in ('boot-one.planner','boot-two.planner'):
                daemon._default_handler(self.ledger,self.root,state,{},
                    {'op':'seat.register','args':{'address':name,'role_word':'Planner'}})
            for ident in ('one','two'):
                daemon._default_handler(self.ledger,self.root,state,{},
                    {'op':'submit','args':self.args(admission.body('SITREP',ident=ident),sid=ident)})
        self.assertEqual(fingerprint.call_count,1)
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM routed_notices').fetchone()[0],2)
        self.assertEqual(self.ledger.execute('SELECT DISTINCT rules_version FROM admission_judgments').fetchall(),[('a'*64,)])

    def test_M12_parse_cache_is_bounded_and_returns_defensive_copies(self):
        from relay_engine import rules
        self.assertTrue(hasattr(rules.header_fields,'__name__'))
        self.assertTrue(hasattr(rules,'_header_fields_cached'))
        raw='FROM: one.planner\nTO: one.implementer\n'
        fields=rules.header_fields(raw);fields['FROM']='mutated'
        values=rules.h27_occurrences(raw,'FROM');values.append('mutated')
        self.assertEqual(rules.header_fields(raw)['FROM'],'one.planner')
        self.assertEqual(rules.h27_occurrences(raw,'FROM'),['one.planner'])
        self.assertEqual(rules.header_fields(raw.replace('one.planner','two.planner'))['FROM'],'two.planner')
        self.assertGreaterEqual(rules._header_fields_cached.cache_info().maxsize,16384)
        before=rules._header_fields_cached.cache_info().hits
        rules.header_fields(raw)
        self.assertEqual(rules._header_fields_cached.cache_info().hits,before+1)
