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
    """An effect to check; unparsed targets must be refused or resolved first.

    A single match never authorizes several targets. Compound merges lose the
    pin; ambiguous pushes carry no ref and the ``unparsed`` flag.
    """

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
    """Return fresh JSON data; callers add repository-specific refs and paths.

    ``refused_paths`` names symbolic key roles, not filesystem locations.
    Consumers must resolve those roles to their installed key paths before
    supplying ``classify_tool``'s refused_paths; labels alone protect no files.
    """
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
    value = None
    for index, word in enumerate(words):
        for name in names:
            if word == name and index + 1 < len(words):
                value = words[index + 1]
            elif word.startswith(name + "="):
                value = word[len(name) + 1:]
            elif len(name) == 2 and word.startswith(name) and len(word) > 2:
                value = word[2:]
    return value


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


def _unwrap(words: list[str], depth: int) -> tuple[list[str], list[str], int]:
    assignments = []
    while words:
        if _ASSIGN.match(words[0]):
            assignments.append(words[0])
            words = words[1:]
        elif words[0] in ("if", "then", "elif", "while", "until", "do", "!", "{", "}"):
            words = words[1:]
        elif os.path.basename(words[0]) == "env":
            depth += 1
            if depth > 10:
                raise ValueError("wrapper depth exceeded")
            words = words[1:]
            while words and words[0].startswith("-"):
                option, words = words[0], words[1:]
                if option in ("-u", "--unset", "-C", "--chdir") and words:
                    words = words[1:]
                if option == "--":
                    break
        else:
            break
    return words, assignments, depth


def _command(words: list[str], paths: Sequence[str], depth: int,
             inherited: Sequence[str]) -> Match | None:
    original = words
    try:
        words, assignments, depth = _unwrap(words, depth)
    except ValueError:
        return _unparsed(" ".join(original), paths)
    configuration = [*inherited, *(word for word in assignments if word.startswith("GIT_CONFIG_"))]
    if not words:
        return None
    for word in words:
        for path in paths:
            if os.path.normpath(word) == os.path.normpath(path):
                return Match("key-file-read", path=path)
    program = os.path.basename(words[0])
    args = words[1:]
    if program in ("bash", "sh", "zsh"):
        for index, arg in enumerate(args):
            if arg.startswith("-") and "c" in arg[1:] and index + 1 < len(args):
                return _shell(args[index + 1], paths, depth + 1, configuration)
    if program == "git":
        if "--no-verify" in args or any("core.hookspath" in word.lower() for word in [*original, *configuration]):
            return Match("hook-bypass")
        # Git global options precede the subcommand; none of them consumes push.
        index = 0
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            index += 2 if option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
        if index < len(args) and args[index] == "push":
            positional = []
            tail = args[index + 1:]
            cursor = 0
            while cursor < len(tail):
                word = tail[cursor]
                if word in ("--repo", "--receive-pack", "--exec", "--push-option", "-o"):
                    cursor += 2
                    continue
                if not word.startswith("-"):
                    positional.append(word)
                cursor += 1
            refs = positional if _option(tail, "--repo") else positional[1:]
            ref = refs[0].rsplit(":", 1)[-1].lstrip("+") if len(refs) == 1 else None
            flags = frozenset({"unparsed"}) if ref is None else frozenset()
            return Match("protected-push", ref=ref, flags=flags)
    if program == "gh":
        repository = _option(args, "--repo", "-R")
        # gh permits its repository/hostname global options before the command.
        while args and args[0].startswith("-"):
            args = args[2:] if args[0] in ("--repo", "-R", "--hostname") else args[1:]
        if args[:2] == ["pr", "merge"]:
            tail = args[2:]
            pr = int(tail[0]) if tail and tail[0].isdigit() else None
            head = _head(_option(tail, "--match-head-commit"))
            flags = {flag for flag in ("auto", "admin")
                     if any(word == "--" + flag or word.startswith("--" + flag + "=") for word in tail)}
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


def _comment_start(text: str, index: int) -> bool:
    return text[index] == "#" and (index == 0 or text[index - 1].isspace()
                                   or text[index - 1] in ";&|(){}")


def _backtick_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == "`":
            return index
        else:
            index += 1
    raise ValueError("unclosed command substitution")


def _substitution_end(text: str, start: int) -> int:
    """Find a $() boundary without letting quoted/comment parentheses close it."""
    contexts = [None]
    quote = None
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif char == "`":
            index = _backtick_end(text, index)
        elif char == "$" and text[index:index + 2] == "$(":
            contexts.append(quote)
            quote = None
            index += 1
        elif char == "(" and quote is None:
            contexts.append(None)
        elif char == ")" and quote is None:
            quote = contexts.pop()
            if not contexts:
                return index
        elif quote is None and _comment_start(text, index):
            newline = text.find("\n", index)
            if newline < 0:
                break
            index = newline
        index += 1
    raise ValueError("unclosed command substitution")


def _shell_parts(text: str) -> tuple[str, list[str]]:
    """Separate executable substitutions and discard non-executable comments."""
    cleaned, substitutions = [], []
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            if text[index:index + 2] != "\\\n":
                cleaned.append(text[index:index + 2])
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif char == "$" and text[index:index + 2] == "$(":
            end = _substitution_end(text, index + 1)
            substitutions.append(text[index + 2:end])
            cleaned.append("__adt_substitution__")
            index = end + 1
            continue
        elif char == "`":
            end = _backtick_end(text, index)
            substitutions.append(text[index + 1:end])
            cleaned.append("__adt_substitution__")
            index = end + 1
            continue
        elif quote is None and _comment_start(text, index):
            newline = text.find("\n", index)
            if newline < 0:
                break
            index = newline
            char = "\n"
        cleaned.append(char)
        index += 1
    return "".join(cleaned), substitutions


def _shell(text: str, paths: Sequence[str], depth: int = 0,
           inherited: Sequence[str] = ()) -> Match | None:
    if depth > 10:
        return _unparsed(text, paths)
    try:
        cleaned, substitutions = _shell_parts(text)
        path = _refused(cleaned, paths)
        if path:
            return Match("key-file-read", path=path)
        lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=";&|(){}\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return _unparsed(text, paths)
    segment = []
    matches = [match for command in substitutions
               if (match := _shell(command, paths, depth + 1, inherited)) is not None]
    for word in [*words, ";"]:
        if all(char in ";&|(){}\n" for char in word):
            match = _command(segment, paths, depth, inherited)
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
    if len(matches) > 1:
        return Match("protected-push", flags=frozenset({"compound", "unparsed"}))
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
