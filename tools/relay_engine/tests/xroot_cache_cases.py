"""Small commit sequences and complete answers for the B9 cache differential."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import hashlib
import time
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
from relay_engine.tests.xroot_b9_cases import DESIGN, REVIEW, O, R, _object_tree
from relay_engine.tests import xroot_narrow_corpus as corpus
import xrootfixgen as xf

BASELINE_SHA256 = 'c1c31f872e2fec0de8f68a580e42e951d44b2cbd9096c6d0102fe8b190553bff'


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 25, tzinfo=tz)


def files():
    return {O: DESIGN, R: REVIEW, 'designs/d1.md': b'design'}


def pin(repo, graph):
    commit = xf._literal_commit(repo, _object_tree(repo, graph))
    return SimpleNamespace(repository=repo, commit=commit, declaration=dict(
        DESIGN_LOCK_ID='d1', DESIGN_RECORD_KIND='design-doc', DESIGN_DOC_ID='d1',
        DESIGN_OWNER='f', DESIGN_SOURCE_REPO=str(repo), DESIGN_SOURCE_COMMIT=commit,
        DESIGN_SOURCE_ROOT='relays', DESIGN_SOURCE_PATH='designs/d1.md',
        DESIGN_SHA256=hashlib.sha256(b'design').hexdigest()))


def make(home, graph=None):
    repo = Path(home)
    xf._init_repo(repo)
    return pin(repo, files() if graph is None else graph)


@contextmanager
def budget(module, seconds=float('inf')):
    # This helper also runs concurrently: never replace the module ContextVar.
    token = module._XROOT_BUDGET.set((time.monotonic()+seconds, []))
    try:
        with corpus.unbounded_selectors():
            yield
    finally:
        module._XROOT_BUDGET.reset(token)


@contextmanager
def authority_fixture(declaration):
    fixture = TestAuthorityFold5()
    fixture.setUp()
    try:
        xf._git(Path(fixture.root.path), 'init', '-q', '-b', 'main')
        artifact = fixture.artifact('plans', 'PL-base')
        fields = '\n'.join(k+': '+v for k,v in declaration.items())
        fixture.add('PLAN', ident='plan', extra=fields+'\nPLAN_LOCK_ID: PL\n'+artifact, content='')
        fixture.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                    'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+artifact+'\nVERDICT: approve', '')
        pair = fixture.add(ident='pair', extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL')
        direct = fixture.add('MERGE-GATE', 'operator', ident='direct',
                             extra='PARENT_DISPATCH_ID: plan', content='DISPATCH MERGE')
        yield fixture, pair, direct
    finally:
        fixture.tearDown()


def whole_answers(declaration, require_external=False):
    with authority_fixture(declaration) as (fixture, pair, direct):
        answers = dict(pair=fixture.check(pair), direct=fixture.check(direct, action='merge'))
        if require_external:
            for answer in answers.values():
                assert 'external' in answer
                assert isinstance(answer['external'], list)
        return answers


def observe(module, consumer, case, *, require_external=False):
    with budget(module):
        return dict(verify=corpus.verify(module, consumer, case.declaration),
                    authority=whole_answers(case.declaration, require_external))
