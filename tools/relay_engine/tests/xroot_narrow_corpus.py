"""Reusable real-object corpus and whole-answer observations for narrow reads."""
from contextlib import contextmanager, ExitStack
import math
import selectors
import threading
import time
from pathlib import Path
from unittest import mock
from relay_engine import rules
from relay_engine.tests import xroot_b9_cases as cases
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
import xrootfixgen as xf


class Budget:
    """Shared proxy; all actual deadlines and nesting live in ContextVars.

    Only implicit authority defaults are lifted by AuthorityTime below.
    Explicit writes are never rewritten, including expiry values other than zero.
    """
    def __init__(self, original, seconds=float('inf')):
        from contextvars import ContextVar
        self.original = original
        self.scopes = ContextVar('semantic_lift_scope', default=0)
    def set(self, value):
        return self.original.set(value)
    def get(self):
        return self.original.get()
    def reset(self, token):
        self.original.reset(token)


_BUDGET_LOCK = threading.Lock()
_BUDGET_SCOPES = {}
_AUTHORITY_TIME = None


class AuthorityTime:
    def __init__(self, original):
        self.original = original
    def __getattr__(self, name):
        return getattr(self.original, name)
    def monotonic(self):
        from relay_engine import authority
        budget = authority.rules._XROOT_BUDGET
        if isinstance(budget, Budget) and budget.scopes.get():
            current = budget.get()
            if current is not None:
                # _external_evidence's fixed +5 default must preserve the
                # active semantic scope, including an explicit finite expiry.
                return current[0] - 5
        return self.original.monotonic()


_SELECTOR_LOCK = threading.Lock()
_SELECTOR_SCOPES = 0
_SELECTOR_PATCHES = None


@contextmanager
def unbounded_selectors():
    """Translate the test-only infinite deadline to the selector's no-timeout API.

    Production deadlines and explicitly injected finite deadlines are untouched.
    Patching both implementations also covers the pinned posix_spawn oracle.
    """
    global _SELECTOR_SCOPES, _SELECTOR_PATCHES
    # Worker scopes may overlap without nesting. Only the last exit restores
    # the shared methods; each worker's actual budget remains a ContextVar.
    with _SELECTOR_LOCK:
        if not _SELECTOR_SCOPES:
            _SELECTOR_PATCHES = ExitStack()
            for cls in {selectors.PollSelector, selectors.DefaultSelector}:
                original = cls.select
                def select(self, timeout=None, original=original):
                    from relay_engine.tests.xroot_test_stops import wait_timeout
                    timeout = None if timeout is not None and math.isinf(timeout) else timeout
                    answer = original(self, wait_timeout(timeout))
                    wait_timeout(None)
                    return answer
                _SELECTOR_PATCHES.enter_context(mock.patch.object(cls, 'select', select))
            original_sleep = time.sleep
            def sleep(seconds):
                from relay_engine.tests.xroot_test_stops import wait_timeout
                original_sleep(wait_timeout(seconds))
                wait_timeout(None)
            # xroot_git polls a live child with no open streams using sleep.
            # That branch must observe the worker's stop as well as selectors.
            _SELECTOR_PATCHES.enter_context(mock.patch.object(time, 'sleep', sleep))
        _SELECTOR_SCOPES += 1
    try:
        yield
    finally:
        with _SELECTOR_LOCK:
            _SELECTOR_SCOPES -= 1
            if not _SELECTOR_SCOPES:
                _SELECTOR_PATCHES.close()
                _SELECTOR_PATCHES = None


@contextmanager
def budget(module, seconds=float('inf')):
    from relay_engine import authority
    from relay_engine.tests.xroot_test_stops import stop
    global _AUTHORITY_TIME
    with _BUDGET_LOCK:
        if module not in _BUDGET_SCOPES:
            original = module._XROOT_BUDGET
            proxy = Budget(original)
            _BUDGET_SCOPES[module] = [original, proxy, 0]
            module._XROOT_BUDGET = proxy
        state = _BUDGET_SCOPES[module]
        state[2] += 1
        if _AUTHORITY_TIME is None:
            _AUTHORITY_TIME = authority.time
            authority.time = AuthorityTime(_AUTHORITY_TIME)
    proxy = state[1]
    token = scope_token = None
    try:
        current = proxy.get()
        if proxy.scopes.get() and (math.isfinite(seconds) or
                                   current is not None and math.isfinite(current[0])):
            raise AssertionError('explicit finite nested corpus budget requires a separate timed scope')
        scope_token = proxy.scopes.set(proxy.scopes.get()+1)
        token = proxy.set((time.monotonic()+seconds, []))
        # Four hours is above the retained slowest large semantic run.
        # This stop only raises; it never supplies a compared answer.
        with unbounded_selectors(), stop(14400):
            yield
    finally:
        if token is not None:
            proxy.reset(token)
        if scope_token is not None:
            proxy.scopes.reset(scope_token)
        with _BUDGET_LOCK:
            state[2] -= 1
            if not state[2]:
                module._XROOT_BUDGET = state[0]
                del _BUDGET_SCOPES[module]
            if not _BUDGET_SCOPES:
                authority.time = _AUTHORITY_TIME
                _AUTHORITY_TIME = None


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
