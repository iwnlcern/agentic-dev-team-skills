"""Capture B9 old answers once, using explicitly pinned rules and authority bytes."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

BASE = 'b8d87ebe15fbd3f5e06f1d0112fccad832acd1b1'


def load(name, path):
    import relay_engine
    spec = importlib.util.spec_from_file_location('relay_engine.' + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    setattr(relay_engine, name, module)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rules', type=Path, required=True)
    parser.add_argument('--authority', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--only', nargs='+')
    parser.add_argument('--cases-root', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('capture output already exists; preserve it and choose a new evidence path')
    hashes = {}
    for name in ('rules', 'authority'):
        path = getattr(args, name)
        original = subprocess.check_output(['git', 'show', f'{BASE}:tools/relay_engine/{name}.py'])
        if path.read_bytes() != original:
            parser.error(f'{path} differs from {BASE}')
        hashes[name] = hashlib.sha256(original).hexdigest()
    rules = load('rules', args.rules)
    authority = load('authority', args.authority)
    from relay_engine.tests.xroot_b9_cases import materialize, deadline_fault
    from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
    import xrootfixgen as xf
    cases = materialize(args.cases_root, only=args.only)
    result = dict(source_commit=BASE, source_sha256=hashes,
                  interpreter=sys.executable, python=platform.python_version(),
                  load_average=os.getloadavg(), page_cache='uncontrolled', cases={})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for label, case in cases.items():
        row = result['cases'][label] = dict(expected_old=case.expected_old, paths={})
        for direct in (False, True):
            fixture = TestAuthorityFold5()
            fixture.setUp()
            try:
                xf._git(Path(fixture.root.path), 'init', '-q', '-b', 'main')
                pin = fixture.artifact('plans', 'PL-base')
                extra = '\n'.join(k + ': ' + v for k, v in case.declaration.items())
                fixture.add('PLAN', ident='plan', extra=extra+'\nPLAN_LOCK_ID: PL\n'+pin, content='')
                fixture.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                            'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+pin+'\nVERDICT: approve', '')
                grant = (fixture.add('MERGE-GATE', 'operator', ident='grant', extra='PARENT_DISPATCH_ID: plan', content='DISPATCH MERGE')
                         if direct else fixture.add(ident='grant', extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL'))
                with deadline_fault(case, rules):
                    started = time.monotonic()
                    answer = authority.check(fixture.ledger, fixture.root, grant.rendered_path,
                                             'p.implementer', 'merge' if direct else 'implement')
                    measured = time.monotonic()-started
                    entry = dict(allowed=answer['allowed'], reasons=answer['reasons'], seconds=measured)
                    token = rules._XROOT_BUDGET.set((time.monotonic()+5, []))
                    try:
                        origin, review, population = rules.xroot_verify(Path(fixture.root.path), case.declaration)
                        entry.update(axis=None, detail=None, origin=origin.name, review=review.name,
                                     population=[r.name for r in population])
                    except rules.XrootAxisError as exc:
                        entry.update(axis=exc.axis, detail=exc.detail.replace(str(case.repository), '<repository>'))
                    finally:
                        rules._XROOT_BUDGET.reset(token)
                    row['paths']['direct' if direct else 'pair'] = entry
                    print(label, 'direct' if direct else 'pair', entry['reasons'], entry['axis'], round(measured, 3), flush=True)
            finally:
                fixture.tearDown()
            args.out.write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')


if __name__ == '__main__':
    main()
