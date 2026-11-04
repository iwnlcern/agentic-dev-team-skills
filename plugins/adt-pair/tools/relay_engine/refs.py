"""v3 references (DD-v3-engine § 6): one spelling per target, checked at submit.

Git is reached only through the ``rules`` module's seam, called by module
attribute (``rules.xroot_git(...)``), so a patch on ``rules`` reaches every
call made here; ``rules.py`` itself is unchanged by this module.
"""

from dataclasses import dataclass
import hashlib
import errno
import os
from pathlib import Path
import re
import stat

from relay_engine import errors
from relay_engine import rules


@dataclass(frozen=True)
class RefSpec:
    kind: str  # "relay", "git" or "path"
    text: str
    relay: str | None
    repo: str | None
    rev: str | None
    path: str | None
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True)
class ResolvedGit:
    commit: str
    blob_sha: str
    line_count: int


_HANDLE = re.compile(r"#[1-9][0-9]{0,17}(?:@[a-z0-9][a-z0-9-]{0,31})?")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_LINES = re.compile(r"#L([1-9][0-9]{0,8})(?:-L([1-9][0-9]{0,8}))?\Z")


def _display(text, limit=80):
    out = []
    for char in text[:limit]:
        code = ord(char)
        if code < 32 or 127 <= code <= 159 or char in "  ":
            out.append("\\u%04x" % code)
        else:
            out.append(char)
    if len(text) > limit:
        out.append("...")
    return "".join(out) or "(empty)"


def _spelling(text, reason):
    raise errors.error_for("E-REF-SPELLING", value=_display(text),
                           reason=_display(reason, 200))


def _unresolved(text, reason):
    raise errors.error_for("E-REF-UNRESOLVED", value=_display(text),
                           reason=_display(reason, 300))


def _lines_suffix(spec):
    if spec.line_start is None:
        return ""
    if spec.line_end == spec.line_start:
        return "#L%d" % spec.line_start
    return "#L%d-L%d" % (spec.line_start, spec.line_end)


def render_ref(spec):
    if spec.kind == "relay":
        return spec.relay
    if spec.kind == "path":
        return "file:" + spec.path
    return "%s@%s:%s%s" % (spec.repo, spec.rev, spec.path, _lines_suffix(spec))


def parse_ref(text):
    """Parse one REF value; refuse any spelling that does not re-render to itself."""
    if not isinstance(text, str):
        raise TypeError("reference text required")
    if text.startswith("#") or _UUID.fullmatch(text):
        if not (_HANDLE.fullmatch(text) or _UUID.fullmatch(text)):
            _spelling(text, "a relay is #<n>, #<n>@<project> or a lowercase UUID")
        return RefSpec("relay", text, text, None, None, None, None, None)
    if text.startswith("file:"):
        path = text[len("file:"):]
        if not (path.startswith("/") or path.startswith("~/")):
            _spelling(text, "a local path is absolute or starts with ~/")
        if any(part in ("", ".", "..") for part in path.split("/")[1:]):
            _spelling(text, "a local path has no empty, . or .. part")
        return RefSpec("path", text, None, None, None, path, None, None)
    repo, at, rest = text.partition("@")
    if not at:
        _spelling(text, "not a relay, Git or file: reference")
    if _NAME.fullmatch(repo) is None:
        _spelling(text, "the repository name is outside [a-z0-9][a-z0-9-]{0,31}")
    rev, colon, tail = rest.partition(":")
    if not colon:
        _spelling(text, "a Git reference is <repo>@<rev>:<path>")
    if not rev or rev.startswith("-") or any(char.isspace() for char in rev):
        _spelling(text, "the revision is empty, starts with -, or holds a space")
    path, line_start, line_end = tail, None, None
    match = _LINES.search(tail)
    if match:
        path = tail[:match.start()]
        line_start = int(match.group(1))
        line_end = int(match.group(2)) if match.group(2) else line_start
        if line_end < line_start:
            _spelling(text, "a line range runs forward")
    if not rules.xroot_valid_relpath(path):
        _spelling(text, "the path is literal and relative: no leading /, "
                        "no empty, . or .. part, no part starting with :")
    spec = RefSpec("git", text, None, repo, rev, path, line_start, line_end)
    rendered = render_ref(spec)
    if rendered != text:
        _spelling(text, "the spelling is not canonical; write " + rendered)
    return spec


def resolve_git(checkout, spec):
    """Resolve a Git reference at its revision; refuse unless it names one file."""
    if spec.kind != "git":
        _unresolved(spec.text, "only a Git reference resolves against a checkout")
    checkout = Path(checkout)
    mode = rules._XROOT_MODE.set(None)
    try:
        try:
            rules._XROOT_MODE.set(rules.xroot_classify(checkout))
        except rules.XrootAxisError as exc:
            _unresolved(spec.text, exc.detail)
        try:
            named = rules.xroot_git(checkout, "rev-parse", "--verify", "--quiet",
                                    "--end-of-options", spec.rev + "^{commit}",
                                    axis="commit")
        except rules.XrootAxisError as exc:
            _unresolved(spec.text, "revision %s does not resolve to a commit: %s"
                        % (spec.rev, exc.detail))
        try:
            repo, commit = rules.xroot_repository_and_commit(
                checkout, {"DESIGN_SOURCE_REPO": str(checkout),
                           "DESIGN_SOURCE_COMMIT": named.strip()})
            tree = rules.xroot_commit_tree(repo, commit)
            if spec.line_start is not None:
                literal = spec.path + _lines_suffix(spec)
                if rules.xroot_walk(repo, tree, literal, "REF") is not None:
                    raise errors.error_for(
                        "E-REF-AMBIGUOUS", value=_display(spec.text),
                        literal=_display(literal), commit=commit)
            entry = rules.xroot_walk(repo, tree, spec.path, "REF")
            if entry is None:
                _unresolved(spec.text, "no file at %s in commit %s" % (spec.path, commit))
            kind, blob = entry
            if kind != "blob":
                _unresolved(spec.text, "%s is a directory or submodule, not a file, "
                            "in commit %s" % (spec.path, commit))
            data = rules.xroot_git(repo, "cat-file", "blob", blob, binary=True)
        except rules.XrootAxisError as exc:
            _unresolved(spec.text, exc.detail)
    finally:
        rules._XROOT_MODE.reset(mode)
    count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    if spec.line_end is not None and spec.line_end > count:
        raise errors.error_for("E-REF-RANGE", value=_display(spec.text),
                               start=spec.line_start, end=spec.line_end,
                               count=count, commit=commit)
    return ResolvedGit(commit, blob, count)


def file_digest(path):
    """sha256 of a local file's bytes; the final component is never a symlink."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _unresolved(str(path), "the path is a symlink")
        _unresolved(str(path), "cannot open: %s" % (exc.strerror or "error"))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _unresolved(str(path), "not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()
