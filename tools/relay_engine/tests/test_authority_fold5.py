"""Real-envelope fixture retained for preserved fold-5 reviewer probes."""
from relay_engine import ledger
from relay_engine.tests import test_authority as h
from relay_engine.tests.test_judging import body
from relay_engine.tests.test_ledger import LedgerFixture, WALL


class TestAuthorityFold5(LedgerFixture):
    artifact = h.TestAuthority.artifact
    check = h.TestAuthority.check

    def setUp(self):
        super().setUp()
        self.initialize()
        ledger.establish_run_identity(self.ledger, 'r')
        self.number = 0
        self.files = {}

    def add(self, phase='IMPL', sender='p.planner', target='p.implementer',
            ident=None, extra='', content='DISPATCH IMPL'):
        self.number += 1
        raw = body(phase, sender, target, ident or f'row-{self.number}', extra, '\n'+content)
        return ledger.admit(self.ledger, self.root, ledger._server_envelope(raw), raw,
                            f'sid-{self.number}', clock=lambda: WALL+self.number)
