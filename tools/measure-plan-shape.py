#!/usr/bin/env python3
"""Measure plan shape and compare threshold classifications with a pinned corpus."""

import argparse
import hashlib
from pathlib import Path

from relay_engine.rules import plan_shape_exceeded, plan_shape_measure


def expected_rows(path: Path) -> dict[str, tuple[str, set[str]]]:
    rows = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) != 10 or cells[0] in {'corpus', '---'}:
            continue
        if not cells[7].isdigit() or len(cells[8]) != 12:
            continue
        names = set() if cells[9] == '-' else set(cells[9].split(','))
        rows[cells[1]] = (cells[8], names)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', type=Path, help='pinned Markdown corpus listing')
    parser.add_argument('files', nargs='+', type=Path)
    args = parser.parse_args()
    expected = expected_rows(args.expect) if args.expect else None
    mismatch = False
    for path in args.files:
        data = path.read_bytes()
        shape = plan_shape_measure(data)
        digest = hashlib.sha256(data).hexdigest()[:12]
        names = {name.replace(' ', '-') for name, _, _ in plan_shape_exceeded(shape)}
        over = ','.join(name for name in
                        ('block', 'fenced', 'lines', 'ratio', 'bytes', 'longest-line')
                        if name in names) or '-'
        label = str(path)
        if expected is None:
            ratio = shape.fenced / shape.lines if shape.lines else 0
            print(f'{label} {shape.lines} {shape.fenced} {ratio:.2f} {shape.largest_block} '
                  f'{shape.byte_count} {shape.longest_line} {digest} {over}')
            continue
        if label not in expected:
            print(f'UNLISTED {label}')
            continue
        old_digest, old_names = expected[label]
        if digest != old_digest:
            print(f'DRIFT {label} {old_digest} {digest}')
        elif names != old_names:
            print(f'MISMATCH {label}')
            mismatch = True
        else:
            print(f'MATCH {label}')
    return 1 if mismatch else 0


if __name__ == '__main__':
    raise SystemExit(main())
