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
                      "gh api REST merge-upstream write",
                      "gh api graphql mergePullRequest/updateRef/updateRefs/mergeBranch/createCommitOnBranch/deleteRef/enqueuePullRequest/enablePullRequestAutoMerge",
                      "gh repo sync", "gh stack merge", "gh extension exec stack merge",
                      "MCP push_files/create_or_update_file/delete_file (protected ref)",
                      "MCP update_pull_request_branch"),
    "hook-bypass": ("git --no-verify", "git core.hooksPath", "GIT_CONFIG_* core.hooksPath"),
    "key-file-read": ("tool string or list input naming a refused path",
                      "content reader naming an ancestor directory",
                      "command naming refused path"),
    "operator-endpoint": ("relay answer", "relay prompt-record"),
    "operator-only": ("adt engine stop", "adt engine configure", "relay engine off", "relay team open"),
}
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_CONFIGURE = re.compile(r"(?<![\w.-])adt['\"]*\s+['\"]*engine['\"]*\s+['\"]*configure(?![\w./-])")
_MUTATIONS = re.compile(r"\b(?:mergePullRequest|updateRef|updateRefs|mergeBranch|"
                        r"createCommitOnBranch|deleteRef|enqueuePullRequest|enablePullRequestAutoMerge)\s*\(")


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
        "flags": {"values": ["pinned", "auto", "admin", "unparsed", "compound"],
                  "unresolved_target": "refuse-or-resolve"},
        # Platform's final names retain distinct stop and restart-request semantics.
        "operator_commands": {"stop": [["adt", "engine", "stop"]],
                              "restart": [["adt", "engine", "restart"]],
                              "configure": [["adt", "engine", "configure"]]},
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


def _head(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _arguments(words: Sequence[str], values: Mapping[str, str] = {},
               switches: Mapping[str, str] = {}) -> tuple[dict[str, list[str]], list[str]]:
    """Consume option values once, so their text cannot become another option."""
    options, positional = {}, []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word == "--":
            positional.extend(words[index:])
            break
        name, separator, value = word.partition("=")
        if name in values:
            if not separator:
                value = words[index] if index < len(words) else ""
                index += 1
            options.setdefault(values[name], []).append(value)
        elif word.startswith("--"):
            options.setdefault(switches.get(name, name), []).append(value if separator else "true")
        elif word.startswith("-") and word != "-":
            for offset, char in enumerate(word[1:], 2):
                short = "-" + char
                if short in values:
                    value = word[offset:]
                    if not value:
                        value = words[index] if index < len(words) else ""
                        index += 1
                    options.setdefault(values[short], []).append(value)
                    break
                options.setdefault(switches.get(short, short), []).append("true")
        else:
            positional.append(word)
    return options, positional


def _last(options: Mapping[str, list[str]], name: str) -> str | None:
    return options[name][-1] if options.get(name) else None


def _expand_home(text: str, home: str | None) -> str:
    if home is None:
        return text
    text = text.replace("${HOME}", home)
    text = re.sub(r"\$HOME\b", lambda _: home, text)
    return re.sub(r"(?<![\w./-])~(?=/|$)", lambda _: home, text)


def _path_match(value: str, paths: Sequence[str], home: str | None = None,
                cwd: str | None = None, content_reader: bool = False) -> Match | None:
    """Compare lexical paths only; unknown roots refuse possible key suffixes."""
    if not value:
        return None
    prefix = re.match(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)", value)
    unresolved = bool(prefix and home is None)
    remainder = value[prefix.end():].lstrip("/") if prefix else value
    candidate = _expand_home(value, home)
    if not unresolved and not os.path.isabs(candidate):
        if cwd is None:
            unresolved = True
        else:
            candidate = os.path.join(cwd, candidate)
    candidate = os.path.normpath(candidate)
    remainder = os.path.normpath(remainder)
    while remainder.startswith("../"):
        remainder = remainder[3:]
    for path in paths:
        target = os.path.normpath(_expand_home(path, home))
        if unresolved:
            targets = [target]
            if content_reader:
                parent = os.path.dirname(target)
                while parent and parent not in targets:
                    targets.append(parent)
                    parent = os.path.dirname(parent)
            if any(item == remainder or item.endswith("/" + remainder) for item in targets) or (
                    content_reader and remainder == "."):
                return Match("key-file-read", path=path, flags=frozenset({"unparsed"}))
        elif candidate == target or (content_reader and target.startswith(candidate.rstrip("/") + "/")):
            return Match("key-file-read", path=path)
    return None


def _leading_options(words: list[str], values: Mapping[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    end = 0
    while end < len(words) and words[end].startswith("-"):
        word = words[end]
        end += 1
        if word in values:
            end += 1
        if word == "--":
            break
    options, _ = _arguments(words[:end], values)
    return options, words[end:]


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
    if _CONFIGURE.search(text):
        effect = "operator-only"
    elif re.search(r"\bgh\b.*\bpr\b.*\bmerge\b", text, re.S):
        effect = "pr-merge"
    elif re.search(r"\bgh\s+(?:repo\s+sync|(?:extension\s+exec\s+)?stack\s+merge)\b", text):
        effect = "api-ref-write"
    elif re.search(r"\bgh\s+api\b", text) and (
            _MUTATIONS.search(text) or re.search(r"(?:-X\s*|--method[=\s]+)(?:PUT|POST|PATCH|DELETE)\b", text)
            or re.search(r"(?:-f|-F|--field|--raw-field)[=\s]*", text)):
        effect = "api-ref-write"
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
                if option in ("-S", "--split-string") and words:
                    words = shlex.split(words[0]) + words[1:]
                    break
                if option.startswith("--split-string="):
                    words = shlex.split(option.split("=", 1)[1]) + words
                    break
                if option in ("-u", "--unset", "-C", "--chdir") and words:
                    words = words[1:]
                if option == "--":
                    break
        elif words[0] in ("<", ">", ">>", "<>", ">&", "<&") and len(words) > 1:
            words = words[2:]
        elif words[0].isascii() and words[0].isdigit() and len(words) > 2 and words[1] in ("<", ">", ">>", ">&", "<&"):
            words = words[3:]
        elif os.path.basename(words[0]) in ("command", "builtin", "exec", "nohup", "time", "nice", "timeout", "stdbuf", "sudo", "xargs"):
            program = os.path.basename(words[0])
            depth += 1
            if depth > 10:
                raise ValueError("wrapper depth exceeded")
            value_options = {
                "exec": ("-a",), "time": ("-f", "-o", "--format", "--output"),
                "nice": ("-n", "--adjustment"), "timeout": ("-s", "--signal", "-k", "--kill-after"),
                "stdbuf": ("-i", "-o", "-e", "--input", "--output", "--error"),
                "sudo": ("-u", "-g", "-h", "-p", "-C", "-T", "-R", "-D", "--user", "--group", "--host", "--prompt", "--chdir"),
                "xargs": ("-n", "-L", "-P", "-I", "-E", "-s", "--max-args", "--max-lines", "--max-procs", "--replace", "--eof", "--max-chars"),
            }.get(program, ())
            _, words = _leading_options(words[1:], {name: name for name in value_options})
            if program == "timeout" and words:
                words = words[1:]
        else:
            break
    return words, assignments, depth


def _command(words: list[str], paths: Sequence[str], depth: int,
             inherited: Sequence[str], cwd: str | None = None) -> Match | None:
    original = words
    try:
        words, assignments, depth = _unwrap(words, depth)
    except ValueError:
        return _unparsed(" ".join(original), paths)
    configuration = [*inherited, *assignments]
    if not words:
        return None
    for word in words:
        match = _path_match(word, paths, cwd=cwd)
        if match:
            return match
    program = os.path.basename(words[0])
    args = words[1:]
    if program == "eval":
        return _shell(" ".join(args), paths, depth + 1, configuration, cwd)
    if program in ("bash", "sh", "zsh", "dash", "ksh"):
        index = 0
        while index < len(args):
            arg = args[index]
            index += 1
            if arg in ("--rcfile", "--init-file", "-o", "-O"):
                index += 1
            elif arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                if index < len(args) and args[index] == "--":
                    index += 1
                return _shell(args[index], paths, depth + 1, configuration, cwd) if index < len(args) else _unparsed(" ".join(words), paths)
        if "<<<" in args:
            index = args.index("<<<")
            if index + 1 < len(args):
                return _shell(args[index + 1], paths, depth + 1, configuration, cwd)
        return _unparsed(" ".join(words), paths)
    if program == "git":
        global_options, command = _leading_options(args, {name: name for name in
            ("-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace")})
        if not command:
            return None
        operation, tail = command[0], command[1:]
        options, positional = _arguments(tail, {name: name for name in
            ("-m", "--message", "-F", "--file", "--grep", "--repo", "--receive-pack", "--exec", "--push-option", "-o")})
        hook_configuration = any(value.split("=", 1)[0].lower() == "core.hookspath"
                                 for name in ("-c", "--config-env") for value in global_options.get(name, []))
        for assignment in configuration:
            key, _, value = assignment.partition("=")
            if re.fullmatch(r"GIT_CONFIG_KEY_[0-9]+", key) and value.lower() == "core.hookspath":
                hook_configuration = True
            if key == "GIT_CONFIG_PARAMETERS" and re.search(r"(?:^|[\s'])core\.hooksPath(?:['\s]*=)", value, re.I):
                hook_configuration = True
        if operation in ("commit", "push", "merge") and (hook_configuration or "--no-verify" in options
                                                        or (operation == "commit" and "-n" in options)):
            return Match("hook-bypass")
        if operation == "config" and positional and positional[0].lower() == "core.hookspath":
            read_only = any(name in options for name in ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"))
            if not read_only and (len(positional) > 1 or any(name in options for name in ("--unset", "--unset-all", "--remove-section"))):
                return Match("hook-bypass")
        if operation == "push":
            refs = positional if "--repo" in options else positional[1:]
            ref = refs[0].rsplit(":", 1)[-1].lstrip("+") if len(refs) == 1 else None
            if ref in ("HEAD", "@"):
                ref = None
            flags = frozenset({"unparsed"}) if ref is None else frozenset()
            return Match("protected-push", ref=ref, flags=flags)
    if program == "gh":
        globals_map = {"--repo": "repo", "-R": "repo", "--hostname": "host"}
        global_options, args = _leading_options(args, globals_map)
        if args and args[0] == "pr":
            pr_options, tail = _leading_options(args[1:], globals_map)
            for key, values in pr_options.items():
                global_options.setdefault(key, []).extend(values)
            args = ["pr", *tail]
        environment = dict(word.split("=", 1) for word in configuration if _ASSIGN.match(word))
        repository = _last(global_options, "repo") or environment.get("GH_REPO")
        if args[:2] == ["pr", "merge"]:
            tail = args[2:]
            options, positional = _arguments(tail, {**globals_map, "--match-head-commit": "head",
                "-t": "subject", "--subject": "subject", "-b": "body", "--body": "body",
                "-F": "body-file", "--body-file": "body-file", "-A": "author-email", "--author-email": "author-email"})
            selector = positional[0] if positional else ""
            url = re.fullmatch(r"https?://[^/]+/([^/]+/[^/]+)/pull/([0-9]+)/?", selector)
            number = re.fullmatch(r"#?([0-9]+)", selector)
            pr = int(url[2]) if url else int(number[1]) if number else None
            head = _head(_last(options, "head"))
            repository = url[1] if url else _last(options, "repo") or repository
            flags = {flag for flag in ("auto", "admin")
                     if _last(options, "--" + flag) not in (None, "false")}
            if pr is None:
                flags.add("unparsed")
            if head:
                flags.add("pinned")
            return Match("pr-merge", repository=repository, pr=pr, head=head, flags=frozenset(flags))
        if args[:2] in (["repo", "sync"], ["stack", "merge"]) or args[:4] == ["extension", "exec", "stack", "merge"]:
            return Match("api-ref-write", repository=repository)
        if args and args[0] == "api":
            options, positional = _arguments(args[1:], {"-X": "method", "--method": "method", "-f": "field",
                "-F": "field", "--field": "field", "--raw-field": "field", "--input": "input",
                "-H": "header", "--header": "header", "--hostname": "host", "-q": "jq", "--jq": "jq", "-t": "template", "--template": "template"})
            method = _last(options, "method")
            fields = "field" in options or "input" in options
            write = (method.upper() not in ("GET", "HEAD", "OPTIONS")) if method else fields
            endpoint = positional[0].lstrip("/") if positional else ""
            queries = [field.split("=", 1)[1] for field in options.get("field", []) if field.startswith("query=")]
            if (endpoint == "graphql" and any(_MUTATIONS.search(query) for query in queries)) or (write and re.match(
                    r"repos/[^/\s]+/[^/\s]+/(?:pulls/[0-9]+/merge|git/refs?(?:/|$)|contents(?:/|$)|merges$|merge-upstream$)", endpoint)):
                return Match("api-ref-write", repository=repository)
    if program.startswith("python") and args:
        if os.path.basename(args[0]) in ("relay", "relay.py"):
            return _command(args, paths, depth + 1, configuration, cwd)
        if args[:2] == ["-m", "relay_engine"]:
            return _command(["relay", *args[2:]], paths, depth + 1, configuration, cwd)
    if program in ("relay", "relay.py"):
        _, args = _leading_options(args, {"--root": "root", "--key": "key"})
        if args[:1] in (["answer"], ["prompt-record"]) or args[:2] in (["authority", "prompt-record"], ["operator", "answer"], ["prompt", "record"]):
            return Match("operator-endpoint")
        if args[:2] in (["engine", "off"], ["team", "open"], ["team", "open-next"]):
            return Match("operator-only")
    if any([program, *args[:len(row) - 1]] == row
           for name in ("stop", "configure") for row in table()["operator_commands"][name]):
        return Match("operator-only")
    fallback = " ".join(words)
    if program in ("rg", "grep"):
        options, positional = _arguments(args, {"-e": "pattern", "--regexp": "pattern"})
        if set(options) <= {"-n", "-F", "pattern"} and ("pattern" in options or positional):
            # Only these known data arguments suppress configure; other named
            # effects and unknown/command-bearing options retain their fallback.
            fallback = _CONFIGURE.sub("__adt_pattern_data__", fallback)
    return _unparsed(fallback, paths) if program not in (
        "git", "gh", "relay", "relay.py", "echo", "printf", "cat") else None


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


def _without_heredocs(text: str) -> str:
    """Keep heredoc headers and shell syntax, removing their non-command data."""
    output, pending = [], []
    quote = None
    contexts = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            output.append(text[index:index + 2])
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif text[index:index + 2] == "$(":
            contexts.append(quote)
            quote = None
            output.append("$(")
            index += 2
            continue
        elif char == ")" and quote is None and contexts:
            quote = contexts.pop()
        elif quote is None and _comment_start(text, index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            output.append(text[index:end])
            index = end
            continue
        elif quote is None and text[index:index + 2] == "<<" and text[index:index + 3] != "<<<":
            match = re.match(r"<<(-?)[ \t]*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))", text[index:])
            if match:
                pending.append((next(value for value in match.groups()[1:] if value is not None), bool(match[1])))
                output.append(match[0])
                index += len(match[0])
                continue
        output.append(char)
        index += 1
        if char == "\n" and pending:
            for delimiter, strip_tabs in pending:
                while index < len(text):
                    end = text.find("\n", index)
                    end = len(text) if end < 0 else end + 1
                    line = text[index:end].rstrip("\n")
                    index = end
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
                else:
                    raise ValueError("unclosed heredoc")
            pending = []
    return "".join(output)


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
           inherited: Sequence[str] = (), cwd: str | None = None) -> Match | None:
    if depth > 10:
        return _unparsed(text, paths)
    try:
        # Heredoc bodies are data for parsing, but still name refused files.
        path = _refused(text, paths)
        if path:
            return Match("key-file-read", path=path)
        cleaned, substitutions = _shell_parts(_without_heredocs(text))
        path = _refused(cleaned, paths)
        if path:
            return Match("key-file-read", path=path)
        lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=";&|(){}<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return _unparsed(text, paths)
    segment = []
    environment = list(inherited)
    directory = cwd
    piped = None
    matches = [match for command in substitutions
               if (match := _shell(command, paths, depth + 1, inherited, cwd)) is not None]
    for word in [*words, ";"]:
        if all(char in ";&|(){}\n" for char in word):
            if segment[:1] in (["export"], ["declare"], ["typeset"]):
                environment.extend(item for item in segment[1:] if _ASSIGN.match(item))
            elif segment and _ASSIGN.match(segment[0]):
                environment.extend(item for item in segment if _ASSIGN.match(item))
            if segment[:1] == ["cd"] and len(segment) > 1:
                directory = os.path.normpath(os.path.join(directory or "", segment[1]))
            if directory:
                for item in segment[1:]:
                    for path in paths:
                        if not item.startswith("-") and os.path.normpath(os.path.join(directory, item)) == os.path.normpath(path):
                            matches.append(Match("key-file-read", path=path))
            if piped is not None and segment and os.path.basename(segment[0]) in ("bash", "sh", "zsh", "dash", "ksh"):
                match = _shell(piped, paths, depth + 1, environment, directory)
            else:
                match = _command(segment, paths, depth, environment, directory)
            if match:
                matches.append(match)
            piped = None
            if word == "|" and segment:
                if segment[0] == "echo":
                    piped = " ".join(segment[1:])
                elif segment[0] == "printf" and len(segment) > 1:
                    piped = " ".join(segment[2:]) if "%s" in segment[1] else segment[1]
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


def _mcp_match(tool_name: str, tool_input: Mapping[str, object],
               protected_refs: Mapping[str, Sequence[str]] | None) -> Match | None:
    if tool_name == "merge_pull_request":
        owner, repo = tool_input.get("owner"), tool_input.get("repo")
        repository = f"{owner}/{repo}" if isinstance(owner, str) and isinstance(repo, str) else None
        number = tool_input.get("pullNumber", tool_input.get("pull_number"))
        pr = int(number) if isinstance(number, (str, int)) and re.fullmatch(r"[0-9]+", str(number)) else None
        head = _head(tool_input.get("expectedHeadSha"))
        return Match("pr-merge", repository=repository, pr=pr, head=head,
                     flags=frozenset({"pinned"}) if head else frozenset())
    if tool_name in ("push_files", "create_or_update_file", "delete_file", "update_pull_request_branch"):
        owner, repo = tool_input.get("owner"), tool_input.get("repo")
        repository = f"{owner}/{repo}" if isinstance(owner, str) and isinstance(repo, str) else None
        if tool_name == "update_pull_request_branch":
            return Match("api-ref-write", repository=repository)
        branch = tool_input.get("branch")
        if not isinstance(branch, str) or not branch or repository is None:
            return Match("api-ref-write", repository=repository, flags=frozenset({"unparsed"}))
        ref = branch if branch.startswith("refs/") else "refs/heads/" + branch
        mapping = protected_refs if protected_refs is not None else {"*": table()["protected_refs"]}
        refs = mapping.get(repository) or mapping.get("*", ())
        if protected(ref, refs):
            return Match("api-ref-write", repository=repository, ref=ref)
    return None


def classify_tool(tool_name: str, tool_input: Mapping[str, object], refused_paths: Sequence[str],
                  home: str | None = None, cwd: str | None = None,
                  protected_refs: Mapping[str, Sequence[str]] | None = None) -> Match | None:
    """Classify with hook-supplied context only; do not read environment or files."""
    name = tool_name.lower().split("__")[-1]
    match = _mcp_match(name, tool_input, protected_refs)
    content_reader = ((name == "grep" and tool_input.get("output_mode") == "content")
                      or (name not in ("glob", "list_directory") and name != "grep"
                          and any(part in name for part in ("search", "read_directory"))))
    for value in tool_input.values():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                key = _path_match(item, refused_paths, home, cwd, content_reader)
                if key:
                    return key
    command = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(command, str):
        return _shell(_expand_home(command, home), refused_paths, cwd=cwd)
    return match
