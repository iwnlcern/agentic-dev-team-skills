"""Place and inspect the chained Git hooks in the locked clone's own directory."""

from dataclasses import dataclass, field
import errno
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
from types import MappingProxyType
from typing import Literal, Mapping

from relay_engine import slips


HOOK_NAMES: tuple[str, ...] = ("reference-transaction", "pre-push")
STUB_MARKER: str = "# adt-kit-hook"
STUB_VERSION: int = 1
GIT_FLOOR: tuple[int, int] = (2, 26)
DECISION_CHILD_S: float = 10.0
RESOLVE_CHILD_S: float = 10.0

RESULT_KINDS: Mapping[str, frozenset[str]] = MappingProxyType({
    "install_hooks": frozenset({
        "installed", "hooks_dir_elsewhere", "hooks_not_a_folder", "local_hook_conflict",
        "hooks_unreadable", "git_too_old",
    }),
    "remove_hooks": frozenset({"removed", "hooks_not_a_folder", "remove_failed"}),
    "inspect_hooks": frozenset({
        "hooks_state", "hooks_dir_elsewhere", "hooks_not_a_folder", "hooks_unreadable", "git_too_old",
    }),
    "resolve_checkout_path": frozenset({"resolved", "not_a_checkout", "git_no_answer"}),
})

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY
_OWN_DIRECTORY = _DIRECTORY | os.O_NOFOLLOW
_SHEBANG = b"#!/bin/sh\n"
_MARKER_PREFIX = (STUB_MARKER + " ").encode("ascii")
_CURRENT_MARKER = _MARKER_PREFIX + str(STUB_VERSION).encode("ascii") + b"\n"


@dataclass(frozen=True)
class Installed:
    written: tuple[str, ...]
    chained: tuple[str, ...]
    kind: str = field(default="installed", init=False)


@dataclass(frozen=True)
class HooksDirElsewhere:
    git_dir: str
    scope: str
    value: str
    kind: str = field(default="hooks_dir_elsewhere", init=False)


@dataclass(frozen=True)
class HooksNotAFolder:
    what: Literal["link", "file"]
    target: str | None
    kind: str = field(default="hooks_not_a_folder", init=False)


@dataclass(frozen=True)
class LocalHookConflict:
    name: str
    files: tuple[str, str]
    kind: str = field(default="local_hook_conflict", init=False)


@dataclass(frozen=True)
class HooksUnreadable:
    message: str
    kind: str = field(default="hooks_unreadable", init=False)


@dataclass(frozen=True)
class GitTooOld:
    version: str
    kind: str = field(default="git_too_old", init=False)


@dataclass(frozen=True)
class Removed:
    done: tuple[tuple[str, Literal["deleted", "restored"]], ...]
    kind: str = field(default="removed", init=False)


@dataclass(frozen=True)
class RemoveError:
    call: str
    name: str
    errno: int | None
    files: tuple[str, str] | None


@dataclass(frozen=True)
class RemoveFailed:
    done: tuple[tuple[str, Literal["deleted", "restored"]], ...]
    remaining: tuple[tuple[str, Literal["stub", "absent", "conflict"]], ...]
    error: RemoveError
    kind: str = field(default="remove_failed", init=False)


@dataclass(frozen=True)
class HooksState:
    missing: tuple[str, ...]
    not_current: tuple[tuple[str, Literal["not-executable", "old-version", "not-a-file"]], ...]
    kind: str = field(default="hooks_state", init=False)


@dataclass(frozen=True)
class ResolvedCheckout:
    git_common_dir: str
    worktree_path: str
    kind: str = field(default="resolved", init=False)


@dataclass(frozen=True)
class ResolveFailure:
    kind: Literal["not_a_checkout", "git_no_answer"]
    git_message: str | None


_STUB_INPUT = r'''
saved="$0.adt-local"
if [ -z "${CLAUDECODE+x}${CLAUDE_CODE_SESSION_ID+x}${CODEX_THREAD_ID+x}${CODEX_SESSION_ID+x}" ]; then
    if [ -x "$saved" ]; then
        exec "$saved" "$@"
    fi
    exit 0
fi

stub_fault() {
    printf '%s\n' @FAULT_REASON@ @FAULT_REMEDY@ >&2
    exit 1
}

input=$(mktemp "${TMPDIR:-/tmp}/adt-hook.XXXXXXXXXX" 2>/dev/null) || stub_fault
trap 'rm -f "$input"' 0
trap 'exit 1' HUP INT TERM
cat 2>/dev/null > "$input" || stub_fault

if [ -x "$saved" ]; then
    "$saved" "$@" < "$input" || exit "$?"
fi
'''

_STUB_LAUNCH = r'''
home_is_absolute() {
    case "${HOME:-}" in
        /*) return 0 ;;
        *) return 1 ;;
    esac
}

status=126
if home_is_absolute && [ -x "$HOME/.adt/bin/adt" ]; then
    # Preserve stdout and the refusal on stderr, suppressing shell diagnostics
    # and any partial stderr output from a launcher that failed.
    exec 3>&1
    launcher_error=$(
        {
            "$HOME/.adt/bin/adt" hook @NAME@ "$@" < "$input" 1>&3 2>&4
        } 4>&1 2>/dev/null
    )
    status=$?
    exec 3>&-
fi
case "$status" in
    0) exit 0 ;;
    10)
        [ -z "$launcher_error" ] || printf '%s\n' "$launcher_error" >&2
        exit 1 ;;
esac

record_or_hash_fault() {
    [ -n "$2" ] || return 1
    home_is_absolute || return 0
    if command -v shasum >/dev/null 2>&1; then
        digest=$(printf '%s' "$2" | shasum -a 256) || return 0
    elif command -v sha256sum >/dev/null 2>&1; then
        digest=$(printf '%s' "$2" | sha256sum) || return 0
    else
        return 0
    fi
    digest=${digest%% *}
    record="$HOME/.adt/sessions/$1-$digest"
    [ -e "$record" ] || [ -L "$record" ]
}

if record_or_hash_fault claude-code "${CLAUDE_CODE_SESSION_ID:-}" ||
   record_or_hash_fault codex "${CODEX_SESSION_ID:-}"; then
    stub_fault
fi
exit 0
'''


def stub_text(name: str) -> bytes:
    if name not in HOOK_NAMES:
        raise ValueError(f"unknown hook: {name}")
    reason, remedy = slips.unavailable_words("fault")
    state_check = '[ "${1:-}" = prepared ] || exit 0\n' if name == "reference-transaction" else ""
    body = _STUB_INPUT + state_check + _STUB_LAUNCH
    body = (body.replace("@NAME@", name)
                .replace("@FAULT_REASON@", shlex.quote(f"refused: {reason} (fault)"))
                .replace("@FAULT_REMEDY@", shlex.quote(f"next: {remedy}; ask operator if you cannot")))
    return _SHEBANG + _CURRENT_MARKER + body.encode("utf-8")


_ANCHORED_GIT = """
import os
import sys

fd = int(sys.argv[1])
os.fchdir(fd)
os.close(fd)
os.execvp("git", ["git", *sys.argv[2:]])
"""

_RESOLVE_GIT = """
import json
import os
import subprocess
import sys
import time

path = sys.argv[1]
deadline = float(sys.argv[2])
answers = []
for flag in ("--git-common-dir", "--show-toplevel"):
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", flag],
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=max(0, deadline - time.monotonic()),
        )
    except (OSError, subprocess.TimeoutExpired):
        sys.exit(1)
    if result.returncode < 0:
        sys.exit(1)
    if result.returncode != 0:
        message = os.fsdecode(result.stderr.removesuffix(b"\\n"))
        kind = "git_no_answer"
        if "not a git repository" in message or "must be run in a work tree" in message:
            kind = "not_a_checkout"
        print(json.dumps({"kind": kind, "git_message": message or None}))
        sys.exit(0)
    answer = os.fsdecode(result.stdout.removesuffix(b"\\n"))
    if not answer:
        sys.exit(1)
    answers.append(answer)
common, top = answers
print(json.dumps({"kind": "resolved", "git_common_dir": os.path.realpath(os.path.join(path, common)),
                  "worktree_path": os.path.realpath(top)}))
"""


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    return env


def _start_child(code: str, args: tuple[str, ...], pass_fds: tuple[int, ...] = ()) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", code, *args],
        env=_git_env(), pass_fds=pass_fds, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _finish_child(child: subprocess.Popen, deadline: float) -> tuple[int, bytes, bytes]:
    with child:
        try:
            stdout, stderr = child.communicate(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise
        return child.returncode, stdout, stderr


def resolve_checkout_path(path: str) -> ResolvedCheckout | ResolveFailure:
    if not os.path.isabs(path):
        raise ValueError("checkout path must be absolute")
    deadline = time.monotonic() + RESOLVE_CHILD_S
    # Leave the isolated child time to reap its own Git on an inner timeout.
    try:
        child = _start_child(_RESOLVE_GIT, (path, str(deadline - 0.2)))
        code, stdout, _ = _finish_child(child, deadline)
        if code != 0:
            return ResolveFailure("git_no_answer", None)
        result = json.loads(stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ResolveFailure("git_no_answer", None)

    if isinstance(result, dict):
        kind = result.get("kind")
        if kind == "resolved":
            common, top = result.get("git_common_dir"), result.get("worktree_path")
            if (isinstance(common, str) and isinstance(top, str)
                    and os.path.isabs(common) and os.path.isabs(top)):
                return ResolvedCheckout(common, top)
        if kind in ("not_a_checkout", "git_no_answer") and "git_message" in result:
            message = result["git_message"]
            if message is None or isinstance(message, str):
                return ResolveFailure(kind, message)
    return ResolveFailure("git_no_answer", None)


def _run_anchored(held_fd: int, deadline: float, *args: str) -> tuple[int, str, str]:
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired("git", DECISION_CHILD_S)
    anchor = os.open(".", _DIRECTORY, dir_fd=held_fd)
    try:
        child = _start_child(_ANCHORED_GIT, (str(anchor), *args), (anchor,))
    finally:
        os.close(anchor)
    code, stdout, stderr = _finish_child(child, deadline)
    return code, os.fsdecode(stdout.removesuffix(b"\n")), os.fsdecode(stderr.removesuffix(b"\n"))


def _git_dirs(held_fd: int) -> tuple[str, ...]:
    try:
        worktrees = os.open("worktrees", _OWN_DIRECTORY, dir_fd=held_fd)
    except FileNotFoundError:
        return (".",)
    try:
        with os.scandir(worktrees) as entries:
            names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    finally:
        os.close(worktrees)
    return (".", *(f"worktrees/{name}" for name in names))


def _is_own_hooks(value: str, held: os.stat_result) -> bool:
    path = value.rstrip("/")
    if not os.path.isabs(path) or os.path.basename(path) != "hooks":
        return False
    try:
        parent = os.stat(os.path.dirname(path))
    except OSError:
        return False
    return (parent.st_dev, parent.st_ino) == (held.st_dev, held.st_ino)


def _decide_hooks_folder(held_fd: int) -> HooksDirElsewhere | HooksUnreadable | GitTooOld | None:
    deadline = time.monotonic() + DECISION_CHILD_S
    try:
        code, version_line, message = _run_anchored(held_fd, deadline, "--version")
        version = version_line.removeprefix("git version ")
        match = re.match(r"(\d+)\.(\d+)", version)
        if code != 0 or not match:
            return HooksUnreadable(message or version_line or "git --version gave no answer")
        if tuple(map(int, match.groups())) < GIT_FLOOR:
            return GitTooOld(version)
        held = os.fstat(held_fd)
        for git_dir in _git_dirs(held_fd):
            config = (f"--git-dir={git_dir}", "config")
            code, value, message = _run_anchored(
                held_fd, deadline, *config, "--type=path", "--get", "core.hooksPath",
            )
            if code == 1 and not value and not message:
                continue
            if code != 0:
                return HooksUnreadable(message or value or "git config gave no answer")
            if _is_own_hooks(value, held):
                continue
            code, scoped, message = _run_anchored(
                held_fd, deadline, *config, "--show-scope", "--type=path", "--get-all",
                "core.hooksPath",
            )
            # Strip the selected value before reading its scope. A newline in
            # the value can otherwise be mistaken for the start of a record.
            if code != 0 or not scoped.endswith(value):
                return HooksUnreadable(message or "git config gave no scope for core.hooksPath")
            scope_tag = scoped.removesuffix(value).rsplit("\n", 1)[-1]
            scope = re.fullmatch(r"(system|global|local|worktree|command|unknown)\t", scope_tag)
            if scope is None:
                return HooksUnreadable("git config gave no scope for core.hooksPath")
            return HooksDirElsewhere(git_dir, scope[1], value)
    except subprocess.TimeoutExpired:
        return HooksUnreadable("git hooks directory decision timed out")
    except OSError as exc:
        return HooksUnreadable(str(exc))
    return None


def _open_hooks(held_fd: int) -> int | HooksNotAFolder | None:
    try:
        return os.open("hooks", _OWN_DIRECTORY, dir_fd=held_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        try:
            return HooksNotAFolder("link", os.readlink("hooks", dir_fd=held_fd))
        except OSError as link_error:
            if link_error.errno != errno.EINVAL:
                raise
            return HooksNotAFolder("file", None)


@dataclass(frozen=True)
class _Entry:
    mode: int
    marker: bytes = b""

    @property
    def kit(self) -> bool:
        return self.marker.startswith(_MARKER_PREFIX) and self.marker[len(_MARKER_PREFIX):].strip().isdigit()


def _lstat(name: str, folder: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _entry(name: str, folder: int) -> _Entry | None:
    info = _lstat(name, folder)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        return _Entry(info.st_mode)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, "rb") as source:
        mode = os.fstat(source.fileno()).st_mode
        if stat.S_ISREG(mode) and source.readline(256) == _SHEBANG:
            return _Entry(mode, source.readline(256))
        return _Entry(mode)


def _local_name(name: str) -> str:
    return f"{name}.adt-local"


def _rename(source: str, target: str, folder: int) -> None:
    os.rename(source, target, src_dir_fd=folder, dst_dir_fd=folder)


def _temporary_name() -> str:
    return ".adt-hook-" + secrets.token_hex(16)


def _stage_stub(name: str, folder: int, temporary: str) -> None:
    content = stub_text(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600, dir_fd=folder)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
    except BaseException:
        os.unlink(temporary, dir_fd=folder)
        raise


def _install_at(folder: int) -> Installed | LocalHookConflict:
    entries = {name: _entry(name, folder) for name in HOOK_NAMES}
    chained = tuple(name for name, entry in entries.items() if entry is not None and not entry.kit)
    for name in chained:
        saved = _local_name(name)
        if _lstat(saved, folder) is not None:
            return LocalHookConflict(name, (name, saved))

    staged: dict[str, str] = {}
    backups: dict[str, str] = {}
    moved: list[str] = []
    written: list[str] = []
    try:
        for name, entry in entries.items():
            temporary = _temporary_name()
            _stage_stub(name, folder, temporary)
            staged[name] = temporary
            if entry is not None and entry.kit:
                backup = _temporary_name()
                os.link(name, backup, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
                backups[name] = backup
        for name in HOOK_NAMES:
            if name in chained:
                _rename(name, _local_name(name), folder)
                moved.append(name)
            _rename(staged[name], name, folder)
            del staged[name]
            written.append(name)
        os.fsync(folder)
    except OSError:
        # Restore local hooks and the original stubs if publication was interrupted.
        for name in reversed(HOOK_NAMES):
            if name in moved:
                _rename(_local_name(name), name, folder)
            elif name in written:
                if name in backups:
                    _rename(backups[name], name, folder)
                    del backups[name]
                else:
                    os.unlink(name, dir_fd=folder)
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            os.unlink(temporary, dir_fd=folder)
    return Installed(HOOK_NAMES, chained)


def install_hooks(held_fd: int) -> Installed | HooksDirElsewhere | HooksNotAFolder | LocalHookConflict | HooksUnreadable | GitTooOld:
    refusal = _decide_hooks_folder(held_fd)
    if refusal is not None:
        return refusal

    created = False
    try:
        folder = _open_hooks(held_fd)
        if folder is None:
            try:
                os.mkdir("hooks", 0o755, dir_fd=held_fd)
                created = True
            except FileExistsError:
                pass
            folder = _open_hooks(held_fd)
        if isinstance(folder, HooksNotAFolder):
            return folder
        if folder is None:
            return HooksUnreadable("hooks directory disappeared before it could be opened")
        try:
            return _install_at(folder)
        finally:
            os.close(folder)
    except OSError as exc:
        if created:
            try:
                os.rmdir("hooks", dir_fd=held_fd)
            except OSError:
                pass
        return HooksUnreadable(str(exc))


def inspect_hooks(held_fd: int) -> HooksState | HooksDirElsewhere | HooksNotAFolder | HooksUnreadable | GitTooOld:
    refusal = _decide_hooks_folder(held_fd)
    if refusal is not None:
        return refusal
    try:
        folder = _open_hooks(held_fd)
        if isinstance(folder, HooksNotAFolder):
            return folder
        if folder is None:
            return HooksState(HOOK_NAMES, ())
        missing = []
        not_current = []
        try:
            for name in HOOK_NAMES:
                entry = _entry(name, folder)
                if entry is None:
                    missing.append(name)
                elif not stat.S_ISREG(entry.mode):
                    not_current.append((name, "not-a-file"))
                elif not entry.mode & 0o111:
                    not_current.append((name, "not-executable"))
                elif entry.marker != _CURRENT_MARKER:
                    not_current.append((name, "old-version"))
        finally:
            os.close(folder)
    except OSError as exc:
        return HooksUnreadable(str(exc))
    return HooksState(tuple(missing), tuple(not_current))


def _remaining_state(name: str, folder: int) -> Literal["stub", "absent", "conflict"] | None:
    entry = _entry(name, folder)
    if entry is not None and entry.kit:
        return "stub"
    if _lstat(_local_name(name), folder) is not None:
        return "absent" if entry is None else "conflict"
    return None


def _remove_at(folder: int) -> Removed | RemoveFailed:
    states = {}
    try:
        for name in HOOK_NAMES:
            states[name] = _remaining_state(name, folder)
    except OSError as exc:
        remaining = tuple((hook, state) for hook, state in states.items() if state is not None)
        return RemoveFailed((), remaining, RemoveError("read", name, exc.errno, None))

    done = []
    error = None
    try:
        for name, state in states.items():
            if state is None:
                continue
            if state == "conflict":
                if error is None:
                    error = RemoveError("conflict", name, None, (name, _local_name(name)))
                continue
            call = "stat"
            if state == "absent" or _lstat(_local_name(name), folder) is not None:
                call = "rename"
                _rename(_local_name(name), name, folder)
                done.append((name, "restored"))
            else:
                call = "unlink"
                os.unlink(name, dir_fd=folder)
                done.append((name, "deleted"))
            states[name] = None
    except OSError as exc:
        error = RemoveError(call, name, exc.errno, None)

    try:
        os.fsync(folder)
    except OSError as exc:
        if error is None or error.call == "conflict":
            error = RemoveError("fsync", "hooks", exc.errno, None)

    if error is not None:
        remaining = tuple((name, state) for name, state in states.items() if state is not None)
        return RemoveFailed(tuple(done), remaining, error)
    return Removed(tuple(done))


def remove_hooks(held_fd: int) -> Removed | HooksNotAFolder | RemoveFailed:
    try:
        folder = _open_hooks(held_fd)
    except OSError as exc:
        return RemoveFailed((), (), RemoveError("open", "hooks", exc.errno, None))
    if isinstance(folder, HooksNotAFolder):
        return folder
    if folder is None:
        return Removed(())
    try:
        return _remove_at(folder)
    finally:
        os.close(folder)
