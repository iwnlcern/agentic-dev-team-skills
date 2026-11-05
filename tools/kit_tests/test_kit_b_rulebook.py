"""B-1 sentence census and judgment-rulebook checks."""

import ast
from collections import Counter
import csv
import io
from pathlib import Path
import re
import subprocess

from test_kit_stage_a import KitTreeCase, ROOT

BASE = "76777cfc08bcdf271d1daacc3bb8b4f49523ccbb"
CENSUS = ROOT / "tools/kit_tests/data/protocol-census-76777cf.tsv"


def split_sentences(text: str) -> list[tuple[int, str]]:
    """Split outside code spans; retain each fenced block as one entry."""
    result = []
    fence = None
    block = []
    for number, raw in enumerate(text.splitlines(), 1):
        mark = re.match(r"^\s*(`{3,}|~{3,})", raw)
        if fence is not None:
            block.append(raw)
            if mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence):
                result.append((opening, "\n".join(block)))
                fence = None
            continue
        if mark:
            fence, opening, block = mark[1], number, [raw]
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        start = cursor = 0
        span = None
        while cursor < len(line):
            if line[cursor] == '`':
                end = cursor
                while end < len(line) and line[end] == '`':
                    end += 1
                run = line[cursor:end]
                if span is None:
                    span = run
                elif span == run:
                    span = None
                cursor = end
                continue
            if span is None and line[cursor] in '.?!' and line[cursor + 1:cursor + 2] == ' ':
                result.append((number, line[start:cursor + 1]))
                cursor += 1
                while cursor < len(line) and line[cursor] == ' ':
                    cursor += 1
                start = cursor
            else:
                cursor += 1
        if start < len(line):
            result.append((number, line[start:]))
    if fence is not None:
        result.append((opening, "\n".join(block)))
    return result


def base_text():
    return subprocess.check_output(['git', 'show', f'{BASE}:shared/protocol.md'], cwd=ROOT, text=True)


def read_census():
    assert CENSUS.is_file(), f"missing census {CENSUS}"
    with CENSUS.open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        assert reader.fieldnames == ['line', 'sentence', 'disposition', 'target']
        return list(reader)


def coverage_delta(rows):
    expected = Counter(split_sentences(base_text()))
    actual = Counter((int(row['line']), row['sentence']) for row in rows)
    return expected - actual, actual - expected


class TestRulebook(KitTreeCase):
    def test_census_covers_every_sentence(self):
        self.assertEqual(coverage_delta(read_census()), (Counter(), Counter()))

    def test_census_drop_control_fails(self):
        rows = read_census()
        self.assertGreater(len(rows), 400)
        removed = rows.pop(len(rows) // 2)
        self.assertEqual(coverage_delta(rows), (Counter({(int(removed['line']), removed['sentence']): 1}), Counter()))

    def test_census_fence_rows(self):
        # Literal control: punctuation inside code, two sentences on one line,
        # and a whole fence (including a blank line) cannot silently disappear.
        fixture = '# Heading\nFirst `a. b` stays. Next? Done!\n```text\nx. y\n\n```\n'
        self.assertEqual(split_sentences(fixture), [(2, 'First `a. b` stays.'), (2, 'Next?'), (2, 'Done!'), (3, '```text\nx. y\n\n```')])
        fences = [(n, text) for n, text in split_sentences(base_text()) if re.match(r'^\s*(`{3,}|~{3,})', text)]
        rows = Counter((int(row['line']), row['sentence']) for row in read_census())
        self.assertGreater(len(fences), 10)
        for entry in fences:
            self.assertEqual(rows[entry], 1, entry)

    def test_census_deleted_names_function(self):
        rows = read_census()
        self.assertTrue(any(row['disposition'] == 'deleted' for row in rows))
        for row in rows:
            self.assertIn(row['disposition'], {'deleted', 'pointer', 'moved', 'rewritten', 'kept'})
            if row['disposition'] != 'deleted':
                continue
            with self.subTest(line=row['line'], sentence=row['sentence'][:70]):
                if re.fullmatch(r'DD-v3-[a-z-]+ § \d+(?:\.\d+)*', row['target']):
                    continue
                match = re.fullmatch(r'`([A-Za-z_]\w*)` in `(tools/[^`]+\.py)`', row['target'])
                self.assertIsNotNone(match, row['target'])
                path = ROOT / match[2]
                definitions = {n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                self.assertIn(match[1], definitions, row['target'])
