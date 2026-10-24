"""Reusable real-object corpus and whole-answer observations for narrow reads."""
from contextlib import contextmanager
import time
from pathlib import Path
from unittest import mock
from relay_engine import rules
from relay_engine.tests import xroot_b9_cases as cases
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
import xrootfixgen as xf


class Budget:
    def __init__(self, original, seconds=600):
        self.original, self.seconds = original, seconds
    def set(self, value):
        return self.original.set((time.monotonic()+self.seconds, value[1]))
    def get(self):
        return self.original.get()
    def reset(self, token):
        self.original.reset(token)


@contextmanager
def budget(module, seconds=600):
    with mock.patch.object(module, '_XROOT_BUDGET', Budget(module._XROOT_BUDGET, seconds)):
        token = module._XROOT_BUDGET.set((0, []))
        try:
            yield
        finally:
            module._XROOT_BUDGET.reset(token)


def materialize(dest):
    """Keep the original fixture inputs, including unengaged and broken edges."""
    rows = []
    for label, root, _, _ in xf.materialize(dest/'shared'):
        fields = rules.header_fields((root/xf.PLAN_NAME).read_text())
        declaration = {key: fields.get(key, '') for key in
                       rules.XROOT_REQUIRED+('DESIGN_LOCK_ID', 'DESIGN_RECORD_KIND')}
        rows.append((label, root, declaration))
    # Injected deadline cases are exercised by test_injected_deadlines_stay_deadline_controls.
    for label, case in cases.materialize(dest/'synthetic', only=[k for k in cases.CASES if not k.startswith('T')]).items():
        rows.append((label, case.repository, case.declaration))
    return rows


def normalize(value, root, declaration):
    if isinstance(value, str):
        repo = declaration.get('DESIGN_SOURCE_REPO', '')
        if repo and Path(repo).is_absolute():
            value = value.replace(repo, '<repository>')
        return value.replace(str(root), '<consumer>')
    if isinstance(value, list):
        return [normalize(v, root, declaration) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, root, declaration) for k,v in value.items()}
    return value


def verify(module, root, declaration):
    """Capture each reader's own raised error, or all retained selection inputs."""
    try:
        origin, review, population = module.xroot_verify(root, declaration)
        assert origin in population and review in population
        return dict(axis=None, detail=None, origin=origin.name, review=review.name,
                    population=[dict(name=r.name, fields=r.fields, text=r.text,
                                     stamp_raw=r.stamp_raw) for r in population])
    except module.XrootAxisError as exc:
        return normalize(dict(axis=exc.axis, detail=exc.detail), root, declaration)


def domain(root, declaration, classify):
    try:
        repo, commit = rules.xroot_repository_and_commit(root, declaration)
        tree, _ = rules.xroot_paths(repo, commit, declaration)
    except rules.XrootAxisError as exc:
        return dict(kind='prewalk', axis=exc.axis, detail=normalize(exc.detail, root, declaration))
    return dict(classify(repo, tree)._asdict())


def authority_answers(declaration):
    fixture = TestAuthorityFold5()
    fixture.setUp()
    try:
        xf._git(Path(fixture.root.path), 'init', '-q', '-b', 'main')
        pin = fixture.artifact('plans', 'PL-base')
        fields = '\n'.join(k+': '+v for k,v in declaration.items())
        fixture.add('PLAN', ident='plan', extra=fields+'\nPLAN_LOCK_ID: PL\n'+pin, content='')
        fixture.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                    'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+pin+'\nVERDICT: approve', '')
        result = {}
        for direct in (False, True):
            grant = (fixture.add('MERGE-GATE', 'operator', ident='direct',
                                 extra='PARENT_DISPATCH_ID: plan', content='DISPATCH MERGE')
                     if direct else fixture.add(ident='pair', extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL'))
            answer = fixture.check(grant, action='merge' if direct else 'implement')
            result['direct' if direct else 'pair'] = {k: answer[k] for k in ('allowed', 'reasons')}
            if 'external' in answer:
                result['direct' if direct else 'pair']['external'] = answer['external']
        return result
    finally:
        fixture.tearDown()
