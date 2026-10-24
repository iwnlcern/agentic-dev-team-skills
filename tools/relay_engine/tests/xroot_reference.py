"""Independent serial narrow walk and old-reader eligibility, harness only.

Traversal derives from b8d87eb xroot_relays; no production batch/cache calls.
"""
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple
from unittest import mock
from relay_engine import rules


def candidate(name: bytes) -> bool:
    return name.startswith(b'DESIGN-') and name.endswith(b'.md')


def xroot_reference_relays(repo: Path, root_tree: str) -> list[rules.XrootRelay]:
    relays = []
    stack = [('tree', root_tree, b'', b'', 0)]
    progress = dict(stopped_at='tree ', relays_read=0, trees_read=0,
                    bytes_read=0, entries_read=0)
    def read(kind, oid, axis):
        try:
            return rules.xroot_git(repo, 'cat-file', kind, oid, axis=axis, binary=True)
        except rules.XrootAxisError as exc:
            if exc.axis == 'capacity' and exc.progress is None:
                raise rules._xroot_capacity(root_tree, progress) from exc
            raise
    retained_bytes = 0
    while stack:
        kind, oid, path, name, depth = stack.pop()
        progress['stopped_at'] = kind+' '+path.decode('utf-8', 'surrogateescape')
        if kind == 'tree':
            if depth > rules.XROOT_MAX_TREE_DEPTH:
                raise rules.XrootAxisError('repository', f'DESIGN_SOURCE_ROOT exceeds maximum tree depth {rules.XROOT_MAX_TREE_DEPTH}')
            if progress['trees_read'] >= rules.XROOT_MAX_TREES:
                raise rules._xroot_capacity(root_tree, progress, 'XROOT_MAX_TREES')
            raw = read('tree', oid, 'repository')
            entries = rules._xroot_parse_tree(raw, progress, root_tree)
            for child, (child_kind, child_oid) in sorted(entries.items(), reverse=True):
                if candidate(child):
                    try:
                        child.decode('utf-8')
                    except UnicodeDecodeError as exc:
                        raise rules.XrootAxisError('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename') from exc
                child_path = path+b'/'+child if path else child
                stack.append((child_kind, child_oid, child_path, child, depth+1))
            progress['trees_read'] += 1
            continue
        if not candidate(name):
            continue
        try:
            relname = path.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise rules.XrootAxisError('authority-population', 'DESIGN_SOURCE_ROOT contains a non-UTF-8 filename') from exc
        if kind == 'symlink':
            raise rules.XrootAxisError('authority-population', f'DESIGN_SOURCE_ROOT contains symlink carrier {relname}')
        if progress['relays_read'] >= rules.XROOT_MAX_RELAYS:
            raise rules._xroot_capacity(root_tree, progress, 'XROOT_MAX_RELAYS')
        blob = read('blob', oid, 'authority-population')
        if progress['bytes_read']+len(blob) > rules.XROOT_MAX_RELAY_BYTES:
            raise rules._xroot_capacity(root_tree, progress, 'XROOT_MAX_RELAY_BYTES')
        try:
            text = blob.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise rules.XrootAxisError('authority-population', f'authority carrier {relname} is not UTF-8') from exc
        progress['relays_read'] += 1
        progress['bytes_read'] += len(blob)
        if rules.xroot_retained(text):
            retained_bytes += len(blob)
            if retained_bytes > rules.XROOT_MAX_RETAINED_BYTES:
                raise rules._xroot_capacity(root_tree, progress, 'XROOT_MAX_RETAINED_BYTES')
            status, stamp, raw_stamp, _ = rules.filename_timestamp(name.decode())
            relays.append(rules.XrootRelay(relname, rules.header_fields(text), text,
                          stamp if status == 'ok' else None, raw_stamp if status == 'ok' else None))
    return relays


@contextmanager
def xroot_reference_walk():
    with mock.patch.object(rules, 'xroot_relays', xroot_reference_relays):
        yield


class XrootDomainVerdict(NamedTuple):
    kind: str
    path: str | None = None
    axis: str | None = None
    detail: str | None = None


def xroot_old_domain(repo: Path, root_tree: str) -> XrootDomainVerdict:
    stack = [('tree', root_tree, b'', b'', 0)]
    while stack:
        kind, oid, path, name, depth = stack.pop()
        if kind == 'tree':
            try:
                if depth > rules.XROOT_MAX_TREE_DEPTH:
                    raise rules.XrootAxisError('repository', f'DESIGN_SOURCE_ROOT exceeds maximum tree depth {rules.XROOT_MAX_TREE_DEPTH}')
                entries = rules.xroot_tree_entries(repo, oid)
            except rules.XrootAxisError as exc:
                return XrootDomainVerdict('structural', axis=exc.axis, detail=exc.detail)
            for child, (child_kind, child_oid) in sorted(entries.items(), reverse=True):
                child_path = path+b'/'+child if path else child
                try:
                    child.decode('utf-8')
                except UnicodeDecodeError:
                    return XrootDomainVerdict('outside', path=child_path.decode('utf-8', 'surrogateescape'))
                stack.append((child_kind, child_oid, child_path, child, depth+1))
        elif name != b'INDEX.md' and (kind == 'symlink' or name.endswith(b'.md')) and not candidate(name):
            return XrootDomainVerdict('outside', path=path.decode('utf-8', 'surrogateescape'))
    return XrootDomainVerdict('inside')
