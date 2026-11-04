"""Rules data and pure effect classifiers shared by host hooks."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os.path
import re
import shlex


EFFECT_CLASSES = (
    "commit", "protected-push", "pr-merge", "api-ref-write", "hook-bypass",
    "key-file-read", "operator-endpoint", "operator-only",
)
_SHAPES = {
    "commit": ("git pre-commit",),
    "protected-push": ("git pre-push (each ref update)", "git push"),
    "pr-merge": ("gh pr merge", "MCP merge_pull_request"),
    "api-ref-write": ("gh api REST merge/refs/contents/merges write",
                      "gh api graphql mergePullRequest/updateRef/enablePullRequestAutoMerge",
                      "gh repo sync", "gh stack merge"),
    "hook-bypass": ("git --no-verify", "git core.hooksPath", "GIT_CONFIG_* core.hooksPath"),
    "key-file-read": ("Read refused path", "command naming refused path"),
    "operator-endpoint": ("relay answer", "relay prompt-record"),
    "operator-only": ("relay engine off", "relay team open"),
}
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_MUTATIONS = re.compile(r"\b(?:mergePullRequest|updateRef|enablePullRequestAutoMerge)\s*\(")


@dataclass(frozen=True)
class Match:
    effect: str
    repository: str | None = None
    ref: str | None = None
    pr: int | None = None
    head: str | None = None
    flags: frozenset[str] = frozenset()
    path: str | None = None


@dataclass(frozen=True)
class RefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str
    delete: bool
    force: bool | None


def table() -> dict:
    """Return fresh JSON data; callers add repository-specific refs and paths."""
    return {
        "protected_refs": ["refs/heads/main"],
        "refused_paths": ["frank operator key", "Jev key", "prompt-hook key", "other seat keys"],
        "effects": {
            effect: {"command_shapes": list(_SHAPES[effect]),
                     "slip_kind": {"commit": "implement", "pr-merge": "merge"}.get(effect)}
            for effect in EFFECT_CLASSES
        },
    }


def protected(ref: str, protected_refs: Sequence[str]) -> bool:
    canonical = ref if ref.startswith("refs/") else "refs/heads/" + ref
    return any(canonical == (item if item.startswith("refs/") else "refs/heads/" + item)
               for item in protected_refs)


def is_agent_env(env: Mapping[str, str]) -> bool:
    return "CLAUDECODE" in env or "CODEX_THREAD_ID" in env


def parse_prepush(stdin_text: str) -> list[RefUpdate]:
    updates = []
    for line in stdin_text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("pre-push line must have four fields")
        local, local_sha, remote, remote_sha = fields
        if not all(re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", sha)
                   for sha in (local_sha, remote_sha)):
            raise ValueError("pre-push line has an invalid object id")
        updates.append(RefUpdate(local, local_sha, remote, remote_sha,
                                 local == "(delete)" or set(local_sha) == {"0"}, None))
    return updates


def _option(words: Sequence[str], *names: str) -> str | None:
    for index, word in enumerate(words):
        for name in names:
            if word == name and index + 1 < len(words):
                return words[index + 1]
            if word.startswith(name + "="):
                return word[len(name) + 1:]
    return None


def _head(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _refused(text: str, paths: Sequence[str]) -> str | None:
    for path in paths:
        if path and re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])", text):
            return path
    return None


def _unparsed(text: str, paths: Sequence[str]) -> Match | None:
    """A named effect cannot become a pass merely because parsing failed."""
    path = _refused(text, paths)
    if path:
        return Match("key-file-read", path=path, flags=frozenset({"unparsed"}))
    if re.search(r"\bgh\b", text):
        effect = "pr-merge" if re.search(r"\bpr\s+merge\b", text) else "api-ref-write"
    elif re.search(r"\bgit\b.*\bpush\b", text, re.S):
        effect = "protected-push"
    else:
        return None
    return Match(effect, flags=frozenset({"unparsed"}))


def _unwrap(words: list[str]) -> list[str]:
    if words and os.path.basename(words[0]) == "env":
        words = words[1:]
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option in ("-u", "--unset", "-C", "--chdir") and words:
                words.pop(0)
            if option == "--":
                break
    while words and _ASSIGN.match(words[0]):
        words = words[1:]
    return words


def _command(words: list[str], paths: Sequence[str], depth: int) -> Match | None:
    original = words
    words = _unwrap(words)
    if not words:
        return None
    program = os.path.basename(words[0])
    args = words[1:]
    if program in ("bash", "sh", "zsh"):
        for index, arg in enumerate(args):
            if arg.startswith("-") and "c" in arg[1:] and index + 1 < len(args):
                return _shell(args[index + 1], paths, depth + 1)
    if program == "git":
        if "--no-verify" in args or any("core.hookspath" in word.lower() for word in original):
            return Match("hook-bypass")
        # Git global options precede the subcommand; none of them consumes push.
        index = 0
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            index += 2 if option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
        if index < len(args) and args[index] == "push":
            refs = args[index + 1:]
            ref = next((part.rsplit(":", 1)[-1].lstrip("+") for part in reversed(refs)
                        if not part.startswith("-")), None)
            return Match("protected-push", ref=ref)
    if program == "gh":
        repository = _option(args, "--repo", "-R")
        # gh permits its repository/hostname global options before the command.
        while args and args[0].startswith("-"):
            args = args[2:] if args[0] in ("--repo", "-R", "--hostname") else args[1:]
        if args[:2] == ["pr", "merge"]:
            tail = args[2:]
            pr = int(tail[0]) if tail and tail[0].isdigit() else None
            head = _head(_option(tail, "--match-head-commit"))
            flags = {flag for flag in ("auto", "admin") if "--" + flag in tail}
            if head:
                flags.add("pinned")
            return Match("pr-merge", repository=repository, pr=pr, head=head, flags=frozenset(flags))
        if args[:2] in (["repo", "sync"], ["stack", "merge"]):
            return Match("api-ref-write", repository=repository)
        if args and args[0] == "api":
            method = _option(args, "-X", "--method")
            fields = any(word in ("-f", "-F", "--field", "--raw-field", "--input")
                         or word.startswith(("--field=", "--raw-field=", "--input=", "-f", "-F"))
                         for word in args[1:])
            write = (method.upper() not in ("GET", "HEAD", "OPTIONS")) if method else fields
            text = " ".join(args[1:])
            if _MUTATIONS.search(text) or (write and re.search(
                    r"(?:^|[\s/])repos/[^/\s]+/[^/\s]+/(?:pulls/\d+/merge|git/refs?(?:/|\b)|contents(?:/|\b)|merges\b)", text)):
                return Match("api-ref-write", repository=repository)
    if program in ("relay", "relay.py"):
        if "answer" in args or "prompt-record" in args or any(
                args[index:index + 2] == ["prompt", "record"] for index in range(len(args))):
            return Match("operator-endpoint")
        if args[:2] == ["engine", "off"] or args[:2] in (["team", "open"], ["team", "open-next"]):
            return Match("operator-only")
    return None


def _shell(text: str, paths: Sequence[str], depth: int = 0) -> Match | None:
    path = _refused(text, paths)
    if path:
        return Match("key-file-read", path=path)
    if depth > 10:
        return _unparsed(text, paths)
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return _unparsed(text, paths)
    segment = []
    matches = []
    for word in [*words, ";"]:
        if all(char in ";&|()\n" for char in word):
            match = _command(segment, paths, depth)
            if match:
                matches.append(match)
            segment = []
        else:
            segment.append(word)
    # An always-refused effect cannot hide behind a merge that might be allowed.
    for match in matches:
        if match.effect not in ("pr-merge", "protected-push"):
            return match
    if len(matches) > 1 and any(match.effect == "pr-merge" for match in matches):
        # One Match cannot authorize two targets. Let the unpinned-merge rule refuse it.
        return Match("pr-merge", flags=frozenset({"compound", "unparsed"}))
    return matches[0] if matches else None


def classify_tool(tool_name: str, tool_input: Mapping[str, object], refused_paths: Sequence[str]) -> Match | None:
    if "merge_pull_request" in tool_name.lower():
        owner, repo = tool_input.get("owner"), tool_input.get("repo")
        repository = f"{owner}/{repo}" if isinstance(owner, str) and isinstance(repo, str) else None
        number = tool_input.get("pullNumber", tool_input.get("pull_number"))
        pr = int(number) if isinstance(number, (str, int)) and str(number).isdigit() else None
        head = _head(tool_input.get("expectedHeadSha"))
        return Match("pr-merge", repository=repository, pr=pr, head=head,
                     flags=frozenset({"pinned"}) if head else frozenset())
    if tool_name.lower().split("__")[-1] in ("read", "read_file"):
        path = tool_input.get("file_path", tool_input.get("path"))
        if isinstance(path, str):
            for refused in refused_paths:
                if os.path.normpath(path) == os.path.normpath(refused):
                    return Match("key-file-read", path=refused)
    command = tool_input.get("command", tool_input.get("cmd"))
    return _shell(command, refused_paths) if isinstance(command, str) else None
