"""Keep isolated Python argv literals in tools/ from writing bytecode."""

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import tempfile
import unittest


@dataclass(frozen=True)
class Site:
    relative: str
    line: int
    source_line: str
    bare: bool
    finding: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.relative, self.source_line


_PYTHON = re.compile(r"python[0-9.]*")
EXCEPTIONS: dict[tuple[str, str], int] = {
    ("tools/relay_engine/tests/test_pdc_consumer.py",
     '[sys.executable, "-I", str(PDC_SWEEPER), seat, "--all", "--root", name],'): 1,
}


def _string(element: ast.expr) -> str | None:
    if isinstance(element, ast.Constant) and isinstance(element.value, str):
        return element.value
    return None


def _interpreter(element: ast.expr) -> bool:
    value = _string(element)
    if value is not None:
        return _PYTHON.fullmatch(value.rsplit("/", 1)[-1]) is not None
    return (isinstance(element, ast.Attribute) and element.attr == "executable"
            and isinstance(element.value, ast.Name) and element.value.id == "sys")


def _letter(char: str) -> bool:
    return char.isascii() and char.isalpha()


def _options(elements: list[ast.expr]) -> set[str]:
    options = set()
    i = 0
    while i < len(elements):
        token = _string(elements[i])
        if token is None or len(token) < 2 or token[0] != "-" or not _letter(token[1]):
            break
        cluster = set()
        for position, char in enumerate(token[1:], start=1):
            if not _letter(char):
                return options
            cluster.add(char)
            if char in "cm":
                return options | cluster
            if char in "WX":
                if position == len(token) - 1:
                    i += 1  # The next element is this option's argument.
                break
        options.update(cluster)
        i += 1
    return options


def scan_file(relative: str, source: bytes) -> list[Site]:
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, filename=relative)
    except (UnicodeDecodeError, SyntaxError, ValueError) as error:
        return [Site(relative, 0, "", False, type(error).__name__)]
    lines = text.splitlines()
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for i, element in enumerate(node.elts):
            if _interpreter(element):
                options = _options(node.elts[i + 1:])
                if "I" in options:
                    line = element.lineno
                    sites.append(Site(relative, line, lines[line - 1].strip(), "B" not in options))
                break
    return sorted(sites, key=lambda site: site.line)


def scan_tree(root: Path) -> list[Site]:
    """Scan regular .py files in tools/, excluding __pycache__ directories."""
    sites = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root.parent).as_posix()
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            source = path.read_bytes()
        except OSError as error:
            sites.append(Site(relative, 0, "", False, type(error).__name__))
        else:
            sites.extend(scan_file(relative, source))
    return sites


def unexcepted(sites: list[Site], exceptions: dict[tuple[str, str], int]) -> list[Site]:
    counts = Counter(site.key for site in sites if site.bare)
    return [site for site in sites if site.finding
            or (site.bare and counts[site.key] != exceptions.get(site.key))]


_FORMS = (
    ("[sys.executable, '-I', '-c', x]", True),
    ('["python3", "-I", "-c", x]', True),
    ('["/usr/bin/python3", "-I", "-c", x]', True),
    ("['/usr/bin/env', 'python3', '-IS', '-c', x]", True),
    ('["python3", "-X", "dev", "-I", "-c", x]', True),
    ('["python3", "-I", "-X", "dev", "-c", x]', True),
    ("[sys.executable, '-I', '-c', x, '-B']", True),
    ("[sys.executable, '-I', '-c', 'python3', '-I', '-B']", True),
    ("[sys.executable, '-B', '-I', '-c', 'python3', '-I']", False),
    ("[sys.executable, '-I', '-B', '-c', x]", False),
    ("[sys.executable, '-ISB', '-c', x]", False),
    ("[sys.executable, '-W', 'error', '-I', '-B', '-c', x]", False),
    ('["python3", "-I", "-Wmodule", "-B", "-c", x]', False),
    ('["python3", "-I", "-Wignore::DeprecationWarning", "-B", "-c", x]', False),
    ('["-n", "-L", "-I"]', None),
    ('[data, "-I", "-c", x]', None),
    ('[PYTHON, "-I", "-c", x]', None),
    ("[sys.executable, '--isolated', '-c', x]", None),
    ("[sys.executable, '-m', 'pkg', '-I']", None),
    ("[sys.executable, '-cI', x]", None),
    ("('python3', '-I', '-c', x)", True),
    ("['python3', '-IB!', '-I']", None),
    ("['python3', '-I', '-WI', '-X', '-B', '-c', x]", True),
)
_OUTSIDE = (
    b'command = "python3 -I -c \'pass\'"\n'
    b"shebang = f'#!{sys.executable} -IS\\n'\n"
    b"raw = b'#!/usr/bin/python3 -IS\\n'\n"
)
_DUPLICATED = "['python3', '-I', '-c', x]"


class TestIsolatedLaunchFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sites = scan_tree(Path(__file__).resolve().parents[2])

    def scan_cases(self, files: dict[str, bytes]) -> list[Site]:
        with tempfile.TemporaryDirectory() as name:
            tools = Path(name) / "tools"
            for relative, content in files.items():
                path = tools / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            return scan_tree(tools)

    def test_no_bare_isolated_launch_under_tools(self):
        issues = unexcepted(self.sites, EXCEPTIONS)
        self.assertFalse(issues, "\n" + "\n".join(
            f"{s.relative}:{s.line}: {s.finding or s.source_line}" for s in issues))

    def test_every_exception_matches_its_count(self):
        counts = Counter(site.key for site in self.sites if site.bare)
        for key, expected in EXCEPTIONS.items():
            self.assertEqual(counts[key], expected, key)

    def test_list_site_forms(self):
        sites = self.scan_cases({"forms.py": "\n".join(text for text, _ in _FORMS).encode()})
        self.assertEqual(
            [(site.line, site.source_line, site.bare, site.finding) for site in sites],
            [(line, text, bare, None) for line, (text, bare) in enumerate(_FORMS, 1)
             if bare is not None],
        )

    def test_outside_the_rule_is_not_scanned(self):
        self.assertEqual(self.scan_cases({
            "forms.py": _OUTSIDE, "command.sh": b"python3 -I -c 'pass'\n",
            "launcher": b"#!/usr/bin/python3 -I\n",
            "__pycache__/ignored.py": b"['python3', '-I']\n",
        }), [])

    def test_unreadable_and_unparseable_files_are_findings(self):
        sites = self.scan_cases({"syntax.py": b"x = (\n", "utf8.py": b"\xff"})
        self.assertEqual([(site.relative, site.finding) for site in sites], [
            ("tools/syntax.py", "SyntaxError"), ("tools/utf8.py", "UnicodeDecodeError")])
        self.assertEqual(unexcepted(sites, {}), sites)

    def test_duplicate_exception_line_fails(self):
        sites = self.scan_cases({"duplicate.py": (_DUPLICATED + "\n").encode() * 2})
        exceptions = {("tools/duplicate.py", _DUPLICATED): 1}
        self.assertEqual(unexcepted(sites, exceptions), sites)
        self.assertEqual(unexcepted(sites[:1], exceptions), [])

    def test_scanner_source_has_no_site(self):
        self.assertEqual(scan_file(Path(__file__).name, Path(__file__).read_bytes()), [])
