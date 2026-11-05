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
        parsed = {}
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
                if path not in parsed:
                    parsed[path] = {n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                definitions = parsed[path]
                self.assertIn(match[1], definitions, row['target'])

    def test_census_moved_present(self):
        text = self.canonical_text('shared/protocol.md')
        for row in read_census():
            number = int(row['line'])
            if number == 56:
                # Explicit v3-kit-impl-b1 hold: retain the base sentence until
                # the operator answer is relayed; do not test its future rewrite.
                self.assertIn(row['sentence'], text)
                continue
            if row['disposition'] not in ('moved', 'rewritten', 'kept'):
                continue
            with self.subTest(line=number, disposition=row['disposition']):
                match = re.fullmatch(r'([^:]+):(\d+)(?: (.*))?', row['target'])
                self.assertIsNotNone(match, row['target'])
                path = ROOT / match[1]
                target_line = int(match[2])
                window = '\n'.join(path.read_text().splitlines()[max(0, target_line - 4):target_line + 3])
                expected = match[3] if row['disposition'] == 'rewritten' else row['sentence']
                if row['disposition'] == 'kept':
                    self.assertIn(expected, path.read_text())
                else:
                    self.assertIn(expected, window, row['target'])

    def test_rulebook_size(self):
        self.assertLess(len((ROOT / 'shared/protocol.md').read_bytes()), 15000)

    def test_rulebook_mechanics_headings_absent(self):
        headings = {line.lstrip('#').strip() for line in self.canonical_text('shared/protocol.md').splitlines() if line.startswith('#')}
        forbidden = {'Literal dispatch token', 'Literal merge token', 'Relay transport', 'Delivery mode', 'Relay lint', 'Canonical relay-header schema', 'Artifact identity, location, and integrity', 'Action-claim git-ref rule', 'Hand-off pointer', 'Handoff completeness validator', 'ROLE/FROM discipline', 'SITREP — boot acknowledgment', 'Design-review lineage gate', 'Orchestrator-review visibility gate'}
        self.assertEqual(headings & forbidden, set())

    def test_engine_sentences_point_at_functions(self):
        text = self.canonical_text('shared/protocol.md')
        command = re.compile(r'`(?:tools/)?relay [a-z]')
        citation = re.compile(r'`([A-Za-z_]\w*)` in `(tools/[^`]+\.py)`')
        found = 0
        for line in text.splitlines():
            if not command.search(line):
                continue
            found += 1
            matches = citation.findall(line)
            self.assertTrue(matches or re.search(r'DD-v3-[a-z-]+ § \d+', line), line)
            for name, rel in matches:
                definitions = {n.name for n in ast.walk(ast.parse((ROOT / rel).read_text())) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
                self.assertIn(name, definitions, line)
        self.assertGreaterEqual(found, 3)

    def test_one_working_copy_rule(self):
        text = self.canonical_text('shared/protocol.md')
        self.assertIn('Revise the one working copy of a design or plan in place; do not create a new `-rN` file for each revision.', text.splitlines())
        self.assertIn('Review snapshots and the lock mark belong to DD-v3-engine § 17.', text.splitlines())

    def test_stage_a_pins_follow_census(self):
        rows = [row for row in read_census() if int(row['line']) == 744]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['disposition'], 'deleted')
        self.assertEqual(rows[0]['target'], '`_role_slug` in `tools/relay_engine/ledger.py`')
        self.assertNotIn('`_role_slug`', self.canonical_text('shared/protocol.md'))

    def test_agent_judgments_survive_mechanics_removal(self):
        text = self.canonical_text('shared/protocol.md')
        required = (
            'A digest acknowledgment is legitimate only in its matching approval or direct-override lineage; a register entry alone is not approval.',
            'Set `HUMAN_GATE_REQUIRED` anew for the requested transition: yes only for a fresh operator decision, with its ask; otherwise no with a reason.',
            'After filing, print your FROM and 3–6 summary lines, then terminal RELAY/TO/CC blocks in filing order, with nothing after them.',
            'Route user-visible behavior and broad scope expansion to the operator.',
            'Use the real authoring clock; never invent a timestamp.',
        )
        for sentence in required:
            self.assertIn(sentence, text.splitlines())
        rows = read_census()
        for line in (68, 70, 150, 152, 749, 908):
            self.assertTrue(all(row['disposition'] != 'deleted' for row in rows if int(row['line']) == line), line)

    def test_delivery_names_connect(self):
        files = [*(ROOT / 'skills').rglob('*.md'), *(ROOT / 'shared').rglob('*.md')]
        for path in files:
            text = path.read_text()
            for old in ('relay-monitor.py bind', 'relay-monitor.py status'):
                self.assertNotIn(old, text, str(path.relative_to(ROOT)))
        for rel in ('shared/protocol.md', 'shared/harness-codex.md', 'skills/pair-planner/SKILL.md', 'skills/pair-implementer/SKILL.md'):
            self.assertIn('relay connect', self.canonical_text(rel), rel)

    def test_no_linter_script_named(self):
        paths = [ROOT / 'README.md', *(ROOT / 'skills').rglob('*'), *(ROOT / 'shared').rglob('*')]
        for path in paths:
            if not path.is_file():
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_text(errors='replace')
                for old in ('relay-lint.py', 'relay-lint-fixtures', 'check-relay-lint-fixtures.py', '--index'):
                    self.assertNotIn(old, text)
        unreleased = self.canonical_text('CHANGELOG.md').split('## [Unreleased]\n', 1)[1].split('\n## [', 1)[0]
        for old in ('relay-lint.py', 'relay-lint-fixtures', 'check-relay-lint-fixtures.py', '--index'):
            self.assertNotIn(old, unreleased)

    def test_testing_pointer(self):
        text = self.canonical_text('skills/pair-implementer/SKILL.md')
        practice = ROOT / 'tools/TESTING.md'
        self.assertTrue(practice.is_file())
        self.assertIn('tools/TESTING.md', text)
        everyday = practice.read_text().split('\n## Everyday\n', 1)[1].split('\n## ', 1)[0]
        copied = set(text.splitlines()) & {line for line in everyday.splitlines() if line.strip()}
        self.assertEqual(copied, set())
