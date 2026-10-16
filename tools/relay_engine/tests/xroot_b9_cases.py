"""B9 generated foreign inputs; no consumer repository is modified.

Transition shapes retain the design measurement's inputs. Literal Git objects
also let the differential exercise paths deeper than the filesystem supports.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
from contextlib import contextmanager
from unittest import mock
import time
import xrootfixgen as xf
from relay_engine.tests.test_authority_fold10 import DESIGN, REVIEW

ORCH = DESIGN.replace('ROLE: Pair Planner', 'ROLE: Orchestrator Planner').replace('FROM: f.planner', 'FROM: f.orchestrator-planner')

ORCH_REV = REVIEW.replace('ROLE: Pair Implementer', 'ROLE: Orchestrator Reviewer').replace('FROM: f.implementer', 'FROM: f.orchestrator-reviewer')

O = 'relays/DESIGN-planner-20260801-100000.md'
R = 'relays/DESIGN-REVIEW-implementer-20260801-110000.md'

def base(**over):
    files = {O: DESIGN, R: REVIEW}; files.update(over); return files

CASES = {
 'control-allow': dict(files=base()),
 'T1-timeout-blob': dict(files=base(), slow='cat-file blob'),
 'T1b-timeout-population-blob': dict(files=base(), slow='@review-oid'),
 'T2-timeout-tree': dict(files=base(), slow='cat-file tree'),
 'T3-timeout-commit': dict(files=base(), slow='cat-file -p'),
 'L1-no-origin': dict(files={R: REVIEW}),
 'L2-no-review': dict(files={O: DESIGN}),
 'L3-owner-mismatch': dict(files=base(), decl=dict(DESIGN_OWNER='g')),
 'L4-verdict-must-revise': dict(files=base(**{R: REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise')})),
 'L5-ordering-review-earlier': dict(files={O: DESIGN, 'relays/DESIGN-REVIEW-implementer-20260801-090000.md': REVIEW}),
 'L6-ambiguity-two-origins': dict(files=base(**{'relays/sub/DESIGN-planner-20260801-100000.md': DESIGN})),
 'L7-orchestrator-peer': dict(files={O: ORCH, R: ORCH_REV}),
 'L8-origin-no-timestamp': dict(files={'relays/DESIGN-planner.md': DESIGN, R: REVIEW}),
 'L9-review-wrong-authority': dict(files=base(**{R: REVIEW.replace('AUTHORITY: review-only', 'AUTHORITY: report-only')})),
 'G1-symlink': dict(files=base(), symlink='relays/zz-link.md'),
 'G2-non-utf8-blob': dict(files=base(**{'relays/SITREP-x-20260801-120000.md': b'PHASE: SITREP\n\xff\n'})),
 'G3-non-utf8-name': dict(files=base(), badname=True),
 'G4-phase-conflict-unrelated': dict(files=base(**{'relays/SITREP-x-20260801-120000.md': 'PHASE: DESIGN\nPHASE: SITREP\nDESIGN_DOC_ID: d1\n'})),
 'G5-depth-257': dict(files=base(**{'relays/' + '/'.join(['d']*257) + '/x.md': 'PHASE: SITREP\n'})),
 'G6-gitlink-md': dict(files=base(), gitlink='relays/zz-sub.md'),
 'C1-over-count-20001': dict(gen=lambda: base(**{f'relays/f{i // 150:03d}/SITREP-x-20260801-{i:06d}.md': 'PHASE: SITREP\nnote %d\n' % i for i in range(20001)})),
 'C2-over-bytes-300MiB': dict(gen=lambda: base(**{f'relays/big/SITREP-x-20260801-{i:06d}.md': ('PHASE: SITREP\n' + ('%07d ' % i) * 131072) for i in range(300)})),
 'M1-over-trees-20001': dict(gen=lambda: base(**{f'relays/t{i:05d}/n.txt': 'x' for i in range(20001)})),
 'M2-over-entries-200001': dict(gen=lambda: base(**{f'relays/e{i // 2000:03d}/n{i:06d}.txt': '' for i in range(200001)})),
 'D1-repo-missing': dict(files=base(), decl=dict(DESIGN_SOURCE_REPO='/nonexistent/repo')),
 'D3-commit-short': dict(files=base(), decl=dict(DESIGN_SOURCE_COMMIT='abc')),
 'D4-commit-absent': dict(files=base(), decl=dict(DESIGN_SOURCE_COMMIT='1'*40)),
 'D5-root-missing': dict(files=base(), decl=dict(DESIGN_SOURCE_ROOT='nope')),
 'D6-path-missing': dict(files=base(), decl=dict(DESIGN_SOURCE_PATH='designs/none.md')),
 'D7-digest-mismatch': dict(files=base(), decl=dict(DESIGN_SHA256='0'*64)),
 'D8-digest-malformed': dict(files=base(), decl=dict(DESIGN_SHA256='ABC')),
 'D9-root-dotdot': dict(files=base(), decl=dict(DESIGN_SOURCE_ROOT='../x')),
}

# Expected reason sets transcribed from DD r5 section 6.2 plus erratum 1.
C = ('authority.could-not-evaluate',)
L = ('authority.lineage-incomplete',)
OLD = {
    'control-allow': (), 'T1-timeout-blob': C, 'T1b-timeout-population-blob': C + L,
    'T2-timeout-tree': C, 'T3-timeout-commit': C,
    'L1-no-origin': L, 'L2-no-review': C, 'L3-owner-mismatch': C,
    'L4-verdict-must-revise': C, 'L5-ordering-review-earlier': C,
    'L6-ambiguity-two-origins': L, 'L7-orchestrator-peer': L,
    'L8-origin-no-timestamp': C, 'L9-review-wrong-authority': L,
    'G1-symlink': L, 'G2-non-utf8-blob': L, 'G3-non-utf8-name': L,
    'G4-phase-conflict-unrelated': L, 'G5-depth-257': C, 'G6-gitlink-md': L,
    'C1-over-count-20001': C + L, 'C2-over-bytes-300MiB': (),
    'M1-over-trees-20001': C, 'M2-over-entries-200001': (),
    'D1-repo-missing': C, 'D3-commit-short': C, 'D4-commit-absent': C,
    'D5-root-missing': L, 'D6-path-missing': C,
    'D7-digest-mismatch': ('authority.artifact-changed',),
    'D8-digest-malformed': C, 'D9-root-dotdot': C,
}

@dataclass
class XrootCase:
    repository: Path
    commit: str
    declaration: dict
    expected_old: tuple | None
    row: str
    slow: str | None = None


def _object_tree(repo, files):
    # Build bottom-up without recursive Python calls or working-tree paths.
    nodes = {(): {}}
    for path, content in files.items():
        parts = tuple(path.split('/'))
        for i in range(1, len(parts)):
            nodes.setdefault(parts[:i], {})
        if isinstance(content, tuple):
            nodes[parts[:-1]][parts[-1].encode()] = content
            continue
        raw = content if isinstance(content, bytes) else content.encode()
        nodes[parts[:-1]][parts[-1].encode()] = (b'100644', xf._literal_object(repo, 'blob', raw))
    for path in sorted(nodes, key=len, reverse=True):
        entries = nodes[path]
        raw = b''.join(xf._tree_entry(mode, name, oid) for name, (mode, oid) in sorted(entries.items()))
        oid = xf._literal_object(repo, 'tree', raw)
        if path:
            nodes[path[:-1]][path[-1].encode()] = (b'40000', oid)
        else:
            return oid


def materialize(dest: Path, *, only=None) -> dict[str, XrootCase]:
    result = {}
    for label, source in CASES.items():
        if only is not None and label not in only:
            continue
        repo = dest / label
        xf._init_repo(repo)
        files = dict(source['gen']() if 'gen' in source else source['files'])
        files['designs/d1.md'] = b'design'
        if source.get('special'):
            source['special'](repo, files)
        tree = _object_tree(repo, files)
        # Special entries are introduced into the relay tree directly.
        if source.get('symlink') or source.get('gitlink') or source.get('badname'):
            relays = {k.removeprefix('relays/'): v for k, v in files.items() if k.startswith('relays/')}
            relay_tree = _object_tree(repo, relays)
            raw = xf._git(repo, 'cat-file', 'tree', relay_tree, text=False)
            if source.get('symlink'):
                raw += xf._tree_entry(b'120000', source['symlink'].split('/')[-1].encode(), xf._literal_object(repo, 'blob', b'target'))
            if source.get('gitlink'):
                raw += xf._tree_entry(b'160000', b'zz-sub.md', '2'*40)
            if source.get('badname'):
                raw += xf._tree_entry(b'100644', b'bad\xff.md', xf._literal_object(repo, 'blob', b'PHASE: SITREP\n'))
            relay_tree = xf._literal_object(repo, 'tree', raw)
            designs = _object_tree(repo, {'d1.md': b'design'})
            tree = xf._literal_object(repo, 'tree', xf._tree_entry(b'40000', b'designs', designs) + xf._tree_entry(b'40000', b'relays', relay_tree))
        commit = xf._literal_commit(repo, tree)
        xf._git(repo, 'update-ref', 'refs/heads/main', commit)
        decl = dict(DESIGN_LOCK_ID='d1', DESIGN_RECORD_KIND='design-doc', DESIGN_DOC_ID='d1', DESIGN_OWNER='f',
                    DESIGN_SOURCE_REPO=str(repo), DESIGN_SOURCE_COMMIT=commit, DESIGN_SOURCE_ROOT='relays',
                    DESIGN_SOURCE_PATH='designs/d1.md', DESIGN_SHA256=hashlib.sha256(b'design').hexdigest())
        decl.update(source.get('decl', {}))
        result[label] = XrootCase(repo, commit, decl, OLD.get(label), label, source.get('slow'))
    return result


@contextmanager
def deadline_fault(case, rules):
    """Force the selected real query past its deadline without a child sleeper.

    The real xroot_git still executes and decides the timeout; only the deadline
    supplied to that query changes. Ordinary capacity inputs use the real clock.
    """
    original = rules.xroot_git
    selected = case.slow
    if selected == '@review-oid':
        selected = xf._git(case.repository, 'rev-parse', case.commit + ':' + R)
    def query(repo, *args, **kwargs):
        if selected and selected in ' '.join(args):
            kwargs['deadline'] = time.monotonic() - 1
        return original(repo, *args, **kwargs)
    with mock.patch.object(rules, 'xroot_git', side_effect=query):
        yield


CASES['B1-complete-within-envelope'] = dict(gen=lambda: base(**{
    f'relays/f{i//150:03d}/SITREP-x-20260801-{i:06d}.md': 'PHASE: SITREP\n'
    for i in range(5000)}))
OLD['B1-complete-within-envelope'] = C + L


def _special_graph(kind):
    def add(repo, files):
        empty = xf._literal_object(repo, 'tree', b'')
        if kind == 'tree-commit':
            files['relays/z'] = (b'40000', xf._literal_commit(repo, empty))
        elif kind == 'blob-tag':
            blob = xf._literal_object(repo, 'blob', b'PHASE: SITREP\n')
            tag = xf._literal_object(repo, 'tag', f'object {blob}\ntype blob\ntag t\ntagger f <f@example.invalid> 1 +0000\n\ntag\n'.encode())
            files['relays/z.md'] = (b'100644', tag)
        elif kind == 'nonstandard':
            files['relays/z.md'] = (b'100600', xf._literal_object(repo, 'blob', b''))
        elif kind == 'malformed':
            files['relays/z'] = (b'40000', xf._literal_object(repo, 'tree', b'broken'))
        else:
            files['relays/'+('a' if kind == 'early-missing' else 'z')] = (b'40000', '3'*40)
    return add


CASES.update({
    'F-depth-2500': dict(files=base(**{'relays/'+'/'.join(['d']*2500)+'/x.md': 'PHASE: SITREP\n'})),
    'F-tree-commit': dict(files=base(), special=_special_graph('tree-commit')),
    'F-blob-tag': dict(files=base(), special=_special_graph('blob-tag')),
    'F-nonstandard-mode': dict(files=base(), special=_special_graph('nonstandard')),
    'F-missing-tree': dict(files=base(), special=_special_graph('missing')),
    'F-early-invalid-later-missing': dict(files=base(**{'relays/a.md': b'\xff'}), special=_special_graph('missing')),
    'F-early-invalid-later-malformed': dict(files=base(**{'relays/a.md': b'\xff'}), special=_special_graph('malformed')),
    'F-early-invalid-later-deep': dict(files=base(**{'relays/a.md': b'\xff', 'relays/'+'/'.join(['z']*257)+'/x.md': ''})),
    'F-early-missing-later-invalid': dict(files=base(**{'relays/z.md': b'\xff'}), special=_special_graph('early-missing')),
    'F-stage1-before-stage2': dict(files=base(**{'relays/a.md': b'\xff', 'relays/z.md': 'PHASE: DESIGN\nPHASE: SITREP\n'})),
    'F-phase-before-origin': dict(files=base(**{'relays/A.md': 'PHASE: DESIGN\nPHASE: SITREP\n'})),
    'F-phase-after-origin': dict(files=base(**{'relays/z.md': 'PHASE: DESIGN\nPHASE: SITREP\n'})),
    'F-invalid-before-metadata': dict(gen=lambda: base(**{'relays/a.md': b'\xff', **{f'relays/z{i:05d}/n.txt': '' for i in range(20001)}})),
    'F-early-symlink-over-count': dict(gen=lambda: base(**{f'relays/zz/SITREP-x-20260801-{i:06d}.md': '' for i in range(20001)}), symlink='relays/a-link.md'),
})
