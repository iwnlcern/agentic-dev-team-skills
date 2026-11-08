"""v3 references (DD-v3-engine § 6): one spelling per target, checked at submit.

Git is reached only through the ``rules`` module's seam, called by module
attribute (``rules.xroot_git(...)``), so a patch on ``rules`` reaches every
call made here; ``rules.py`` itself is unchanged by this module.  Every read
runs with replacement objects off, and a process environment holding any
``GIT_`` variable is refused, since either would change Git's answer.
"""

import contextlib
from dataclasses import dataclass
import hashlib
import errno
import fcntl
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
    """Untrusted text with controls escaped; the output is at most limit + 3 characters."""
    out, size = [], 0
    for char in text:
        code = ord(char)
        piece = ("\\u%04x" % code if code < 32 or 127 <= code <= 159 or
                 0xD800 <= code <= 0xDFFF or char in "\u2028\u2029" else char)
        if size + len(piece) > limit:
            out.append("...")
            break
        out.append(piece)
        size += len(piece)
    return "".join(out) or "(empty)"


def _unreadable(text):
    """A control character or an unpaired surrogate anywhere in the spelling."""
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
               for char in text)


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
    if _unreadable(text):
        _spelling(text, "the spelling holds a control character or an unpaired surrogate")
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


def _require_clean_environment(text):
    names = sorted(name for name in os.environ if name.startswith("GIT_"))
    if names:
        raise errors.error_for("E-REF-GIT-ENVIRONMENT", value=_display(text),
                               name=_display(names[0], 60), count=len(names))


@contextlib.contextmanager
def _git_session(checkout, text):
    """Every Git read inside runs with a clean environment and replacement
    objects off (fold R1, R2), and leaves only typed errors naming text."""
    _require_clean_environment(text)
    location = os.path.realpath(Path(checkout))
    try:
        info = os.stat(location)
    except OSError as exc:
        _unresolved(text, "the checkout cannot be read: %s" % (exc.strerror or "error"))
    # replacement objects off for every read of this session, whatever
    # classification would say (rules.xroot_git adds --no-replace-objects)
    mode = rules._XROOT_MODE.set(
        rules.XrootNamespace(location, info.st_dev, info.st_ino))
    try:
        try:
            yield
        except rules.XrootAxisError as exc:
            if exc.axis == "capacity":
                raise errors.error_for("E-REF-TIMEOUT", value=_display(text)) from None
            _unresolved(text, exc.detail)
        except (UnicodeError, ValueError) as exc:
            _unresolved(text, "Git returned text the engine cannot read (%s)"
                        % type(exc).__name__)
    finally:
        rules._XROOT_MODE.reset(mode)


def resolve_git(checkout, spec):
    """Resolve a Git reference at its revision; refuse unless it names one file."""
    if spec.kind != "git":
        _unresolved(spec.text, "only a Git reference resolves against a checkout")
    with _git_session(checkout, spec.text):
        return _resolve(Path(checkout), spec)


def read_blob(checkout, blob_id):
    """The bytes of a blob, read like a resolution."""
    with _git_session(checkout, blob_id):
        return rules.xroot_git(Path(checkout), "cat-file", "blob", blob_id, binary=True)


def changed_paths(checkout, commit):
    """Paths a commit changes against its first parent (all of them for a root)."""
    with _git_session(checkout, commit):
        listed = rules.xroot_git(Path(checkout), "diff-tree", "-r", "-z", "--name-only",
                                 "--no-commit-id", "--root", "-m", "--first-parent",
                                 "--end-of-options", commit, binary=True)
    return {name.decode("utf-8", "surrogateescape") for name in listed.split(b"\0") if name}


def _entry(repo, tree, path):
    """(kind, oid) at path, None when absent, "symlink" when the walk meets one."""
    try:
        return rules.xroot_walk(repo, tree, path, "REF")
    except rules.XrootAxisError as exc:
        if exc.axis == "declaration":
            return "symlink", exc.detail
        raise


def _resolve(checkout, spec):
    try:
        named = rules.xroot_git(checkout, "rev-parse", "--verify", "--quiet",
                                "--end-of-options", spec.rev + "^{commit}",
                                axis="commit")
    except rules.XrootAxisError as exc:
        if exc.axis == "capacity":
            raise
        _unresolved(spec.text, "revision %s does not resolve to a commit: %s"
                    % (spec.rev, exc.detail))
    repo, commit = rules.xroot_repository_and_commit(
        checkout, {"DESIGN_SOURCE_REPO": str(checkout),
                   "DESIGN_SOURCE_COMMIT": named.strip()})
    if rules._XROOT_MODE.get() is None:
        _unresolved(spec.text, "the checkout moved during resolution, so replacement "
                    "objects could not be kept off")
    # the tree by rev-parse, so the commit message is never decoded
    tree = rules.xroot_git(repo, "rev-parse", "--verify", "--end-of-options",
                           commit + "^{tree}", axis="commit").strip()
    entry = _entry(repo, tree, spec.path)
    if entry is not None and entry[0] == "symlink":
        _unresolved(spec.text, entry[1])
    if spec.line_start is not None:
        literal = spec.path + _lines_suffix(spec)
        if _entry(repo, tree, literal) is not None:
            if entry is not None:
                raise errors.error_for(
                    "E-REF-AMBIGUOUS", value=_display(spec.text),
                    literal=_display(literal), commit=commit)
            _unresolved(spec.text, "no file at %s in commit %s; an entry named %s exists, "
                        "which this spelling reads as %s with a line range"
                        % (spec.path, commit, literal, spec.path))
    if entry is None:
        _unresolved(spec.text, "no file at %s in commit %s" % (spec.path, commit))
    kind, blob = entry
    if kind == "tree":
        _unresolved(spec.text, "%s is a directory, not a file, in commit %s"
                    % (spec.path, commit))
    # the entry's own type: a gitlink lists as "commit" whether or not the
    # submodule's commit is in this repository
    listed = rules.xroot_git(repo, "--literal-pathspecs", "ls-tree", "-z", "--full-tree",
                             tree, "--", spec.path, binary=True)
    if listed.split(b" ", 2)[1:2] != [b"blob"]:
        _unresolved(spec.text, "%s is a submodule (a gitlink to another repository), "
                    "not a file, in commit %s" % (spec.path, commit))
    data = rules.xroot_git(repo, "cat-file", "blob", blob, binary=True)
    count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    if spec.line_end is not None and spec.line_end > count:
        raise errors.error_for("E-REF-RANGE", value=_display(spec.text),
                               start=spec.line_start, end=spec.line_end,
                               count=count, commit=commit)
    return ResolvedGit(commit, blob, count)


def file_digest(path):
    """sha256 of a local file's bytes.  The path is absolute (the caller expands
    ~/), the final component is never a symlink, and only a regular file is
    read; the open never waits on a FIFO."""
    return read_file(path, 0)[0]


def read_file(path, keep):
    """(sha256, length, bytes) of a local file in one read, read like
    file_digest; bytes is None when the file is longer than keep."""
    path = Path(path)
    if not path.is_absolute():
        _unresolved(str(path), "the path is not absolute; expand ~/ before digesting")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _unresolved(str(path), "the path is a symlink")
        _unresolved(str(path), "cannot open: %s" % (exc.strerror or "error"))
    except ValueError:
        _unresolved(str(path), "the path holds a NUL character")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _unresolved(str(path), "not a regular file")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        digest, length, kept = hashlib.sha256(), 0, bytearray()
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
            if length <= keep:
                kept += chunk
    finally:
        os.close(fd)
    return digest.hexdigest(), length, bytes(kept) if length <= keep else None
