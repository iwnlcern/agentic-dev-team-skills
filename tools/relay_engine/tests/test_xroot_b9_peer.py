"""B9's sole new peer relation allows, while adjacent wrong tiers stay refused."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from relay_engine import rules
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests.test_xroot import _standalone
from relay_engine.tests.xroot_b9_cases import OLD, CASES
import xrootfixgen as xf


class TestB9Peer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = _standalone()

    def pair(self, origin, review, allowed):
        with tempfile.TemporaryDirectory(prefix='b9-peer-') as td:
            xf._init_repo(Path(td))
            def mutate(repo):
                p = repo / 'relays' / xf.ORIGIN_NAME
                p.write_text(xf.DESIGN.replace('ROLE: Domain Planner', 'ROLE: '+origin.replace('-', ' ').title())
                             .replace('m-1.domain-planner', 'm-1.'+origin))
                p = repo / 'relays' / xf.REVIEW_NAME
                p.write_text(xf.REVIEW.replace('ROLE: Domain Reviewer', 'ROLE: '+review.replace('-', ' ').title())
                             .replace('m-1.domain-reviewer', 'm-1.'+review))
            repo, commit, digest = xf._foreign_repo(Path(td), mutate)
            declaration = dict(xf._plan_fields(repo, commit, digest))
            for module in (rules, self.live):
                with self.subTest(copy=module.__name__, origin=origin, review=review), corpus.budget(module):
                    if allowed:
                        try:
                            selected = module.xroot_verify(Path(td), declaration)
                        except module.XrootAxisError as exc:
                            self.fail(f'{exc.axis}: {exc.detail}')
                        if module is rules:
                            self.assertEqual(xf.ORIGIN_NAME, selected[0].name)
                            self.assertEqual(xf.REVIEW_NAME, selected[1].name)
                    else:
                        with self.assertRaises(module.XrootAxisError) as caught:
                            module.xroot_verify(Path(td), declaration)
                        self.assertEqual('authority-population', caught.exception.axis)
                        self.assertIn('is not a peer', caught.exception.detail)

    def test_orchestrator_pair_allows(self):
        self.pair('orchestrator-planner', 'orchestrator-reviewer', True)

    def test_orchestrator_planner_reviewed_by_implementer_refused(self):
        self.pair('orchestrator-planner', 'implementer', False)

    def test_planner_reviewed_by_orchestrator_reviewer_refused(self):
        self.pair('planner', 'orchestrator-reviewer', False)

    def test_orchestrator_reviewer_origin_refused(self):
        self.pair('orchestrator-reviewer', 'orchestrator-reviewer', False)

    def test_named_absent_pairs_stay_refused(self):
        for origin, review in (
            ('planner', 'orchestrator-reviewer'), ('planner', 'master-reviewer'),
            ('planner', 'planner'), ('planner', 'orchestrator-planner'),
            ('orchestrator-planner', 'implementer'), ('orchestrator-planner', 'planner'),
        ):
            self.pair(origin, review, False)

    def assert_baseline(self, baseline):
        self.assertEqual(set(CASES), set(baseline['cases']))
        expected = {label: list(reasons) for label, reasons in OLD.items()}
        for path in ('pair', 'direct'):
            actual = {label: baseline['cases'][label]['paths'][path]['reasons'] for label in OLD}
            self.assertEqual(expected, actual, path)

    def test_baseline_matches_design_table(self):
        baseline = json.loads((Path(__file__).parents[2] / 'relay-engine-fixtures/xroot/baseline-b8d87eb.json').read_text())
        self.assert_baseline(baseline)
        damaged = copy.deepcopy(baseline)
        damaged['cases']['control-allow']['paths']['pair']['reasons'] = ['authority.could-not-evaluate']
        with self.assertRaises(AssertionError):
            self.assert_baseline(damaged)
