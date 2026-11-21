"""Rules data and pure effect classifiers shared by host hooks."""

from collections.abc import Mapping, Sequence, Callable
from dataclasses import dataclass
import json
import os.path
import re
import shlex


BRIDGE_OPERATION_PREFIX: str = "/api/op/"
LAUNCH_FRAGMENT: str = "#launch="
LAUNCH_PAGE: str = "open.html"

EFFECT_CLASSES = (
    "commit", "protected-push", "pr-merge", "api-ref-write", "hook-bypass",
    "key-file-read", "operator-endpoint", "operator-only", "hook-disable", "top-seat-init",
)
_SHAPES = {
    "commit": ("git reference-transaction (prepared)",),
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
                      "command naming refused path",
                      "console launch page shape under runtime bases"),
    "operator-endpoint": ("relay answer", "relay prompt-record", "adt console open --print",
                          "bridge call address in command text or url input"),
    "operator-only": ("adt engine stop", "adt engine configure", "relay engine off", "relay team open",
                      "adt engine start", "adt engine update", "adt engine install",
                      "adt checkout register", "adt checkout unregister", "adt seat unseat"),
    "hook-disable": ("persistent kit hook disabling write",),
    "top-seat-init": ("adt seat claim",),
}
_BRIDGE_ADDRESS = re.compile(
    r"(?:http://|(?<![A-Za-z0-9.-]))(?:127[.]0[.]0[.]1|localhost):[0-9]{1,5}"
    rf"(?:{re.escape(BRIDGE_OPERATION_PREFIX)}|/{re.escape(LAUNCH_FRAGMENT)})"
)
_RUNTIME_ID = re.compile(r"[0-9a-f]{16}")
_CONSOLE_PRINT_OPTIONS = frozenset(("--p", "--pr", "--pri", "--prin", "--print"))
_CONSOLE_VALUE_OPTIONS = frozenset(
    option[:size]
    for option in ("--ready", "--store-root", "--item")
    for size in range(3, len(option) + 1)
)
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
    The launch page is described separately and uses supplied runtime bases.
    """
    return {
        "design": "DD-v3-authority-20261104-r22",
        "protected_refs": ["refs/heads/main"],
        "refused_paths": ["frank operator key", "Jev key", "prompt-hook key", "other seat keys"],
        "refused_path_shapes": {"console launch page": f"<base>/<16 lowercase hex>/{LAUNCH_PAGE}"},
        "flags": {"values": ["pinned", "auto", "admin", "unparsed", "compound"],
                  "unresolved_target": "refuse-or-resolve"},
        # Platform's final names retain distinct stop and restart-request semantics.
        "operator_commands": {"stop": [["adt", "engine", "stop"]],
                              "restart": [["adt", "engine", "restart"]],
                              "configure": [["adt", "engine", "configure"]],
                              "start": [["adt", "engine", "start"]],
                              "update": [["adt", "engine", "update"]],
                              "install": [["adt", "engine", "install"]],
                              "checkout_register": [["adt", "checkout", "register"]],
                              "checkout_unregister": [["adt", "checkout", "unregister"]],
                              "unseat": [["adt", "seat", "unseat"]],
                              "seat_claim": [["adt", "seat", "claim"]]},
        "effects": {
            effect: {"command_shapes": list(_SHAPES[effect]),
                     "slip_kind": {"commit": "implement", "pr-merge": "merge"}.get(effect),
                     "hook_codes": ['authority.' + code for code in {
                         'commit': [], 'protected-push': ['protected-ref-push'],
                         'pr-merge': ['merge-head-unpinned', 'merge-flag-refused'],
                         'api-ref-write': ['api-merge-refused'], 'top-seat-init': ['operator-only'],
                     }.get(effect, [effect])],
                     "no_seat": 'refuse' if effect in (
                         'hook-disable', 'key-file-read', 'operator-endpoint', 'operator-only') else 'pass'}
            for effect in EFFECT_CLASSES
        },
    }


KIT_LAUNCHER_SUFFIX = '/.adt/bin/adt'


@dataclass(frozen=True)
class ClassifierContext:
    hooks_dir: str | None
    git_config_paths: tuple[str, ...]
    host_config_paths: tuple[str, ...]
    kit_hook_files: tuple[str, ...]
    kit_entry_files: Mapping[str, str]
    resolve: Callable[[str], str]


def kit_entries(settings_text: str, home: str | None) -> frozenset[tuple[str, str, str]] | None:
    """Keep each kit launcher entry's event, matcher and exact command spelling."""
    try:
        settings = json.loads(settings_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(settings, dict):
        return None
    hooks = settings.get('hooks', {})
    if not isinstance(hooks, dict):
        return None
    launchers = {prefix + KIT_LAUNCHER_SUFFIX for prefix in ('~', '$HOME', '${HOME}')}
    if home is not None:
        launchers.add(home.rstrip('/') + KIT_LAUNCHER_SUFFIX)
    entries = set()
    for event, rows in hooks.items():
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('hooks', []), list):
                return None
            matcher = row.get('matcher', '')
            if not isinstance(matcher, str):
                return None
            for hook in row.get('hooks', []):
                if not isinstance(hook, dict):
                    return None
                command = hook.get('command')
                if not isinstance(command, str):
                    continue
                try:
                    words = shlex.split(command)
                except ValueError:
                    continue
                if len(words) >= 2 and words[0] in launchers and words[1] == 'hook':
                    entries.add((event, matcher, command))
    return frozenset(entries)


def _target(value: str, context: ClassifierContext, home: str | None,
            cwd: str | None) -> str | None:
    """None means a target cannot be resolved from the supplied context."""
    if re.match(r'^(?:~|\$HOME|\$\{HOME\})(?=/|$)', value) and home is None:
        return None
    value = _expand_home(value, home)
    if not os.path.isabs(value):
        if cwd is None:
            return None
        value = os.path.join(cwd, value)
    return context.resolve(os.path.normpath(value))


def _hook_target(path: str, context: ClassifierContext) -> bool:
    directory = context.hooks_dir
    return bool(directory and (path == directory or path.startswith(directory.rstrip('/') + '/')))


def _config_write(text: str, fragment: bool = False) -> bool:
    """Recognize Git's core.hooksPath and include paths in written config text."""
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('#', ';')):
            continue
        match = re.match(r'\[([^\]]+)\]', line)
        if match:
            section = match[1].split()[0].lower()
            continue
        key = re.match(r'([\w.-]+)\s*=', line)
        if key:
            key = key[1].lower()
            if (key == 'hookspath' and (section == 'core' or fragment)
                    or key == 'path' and (section == 'include' or section == 'includeif' or fragment)
                    or _hook_config_key(key)):
                return True
    return False


def _host_off(text: str, path: str, fragment: bool = False) -> bool:
    if path.endswith('.toml'):
        section = None
        for line in text.splitlines():
            match = re.match(r'\s*\[([^\]]+)\]', line)
            if match:
                section = match[1].strip()
            elif (section == 'features' or fragment) and re.match(r'\s*hooks\s*=\s*false\b', line):
                return True
        return False
    try:
        value = json.loads(text)
    except ValueError:
        # Edits may carry just the replaced key rather than a whole object.
        return bool(fragment and (re.search(r'"disableAllHooks"\s*:\s*true\b', text)
            or re.search(r'"adt-(?:pair|orchestrator|master)(?:@[^"\s]+)?"\s*:\s*false\b', text)))
    if not isinstance(value, dict):
        return False
    plugin_maps = [value.get('enabledPlugins', {})]
    if fragment:
        plugin_maps.append(value)
    return value.get('disableAllHooks') is True or any(
        isinstance(plugins, dict) and any(setting is False and name.split('@')[0]
        in ('adt-pair', 'adt-orchestrator', 'adt-master') for name, setting in plugins.items())
        for plugins in plugin_maps)


def _hook_config_key(key: str) -> bool:
    key = key.lower()
    return key in ('core.hookspath', 'include.path') or key.startswith('includeif.') and key.endswith('.path')


def _file_disable(name: str, payload: Mapping[str, object], context: ClassifierContext | None,
                  home: str | None, cwd: str | None) -> Match | None:
    if context is None or name not in ('write', 'edit', 'multiedit'):
        return None
    path = payload.get('file_path', payload.get('path'))
    if not isinstance(path, str):
        return None
    target = _target(path, context, home, cwd)
    if target is None:
        return Match('hook-disable')
    current = context.kit_entry_files.get(target)
    fragment = name != 'write'
    if name == 'write':
        text = payload.get('content', '')
    else:
        edits = payload.get('edits', []) if name == 'multiedit' else [payload]
        text = current
        fragments = []
        changed = False
        for edit in edits:
            if not isinstance(edit, Mapping):
                continue
            old, new = edit.get('old_string'), edit.get('new_string')
            if not isinstance(old, str) or not isinstance(new, str):
                continue
            if text is not None:
                if old not in text:
                    continue
                text = text.replace(old, new, -1 if edit.get('replace_all') else 1)
            fragments.append(new)
            changed = True
        if not changed:
            return None
        if text is None:
            text = '\n'.join(fragments)
    if not isinstance(text, str):
        return None
    if _hook_target(target, context) or target in context.kit_hook_files:
        return Match('hook-disable')
    if target in context.git_config_paths and _config_write(text, fragment):
        return Match('hook-disable')
    if target in context.host_config_paths and _host_off(text, target, fragment):
        return Match('hook-disable')
    if current is not None:
        before, after = kit_entries(current, home), kit_entries(text, home)
        if after is None or before is not None and not before <= after:
            return Match('hook-disable')
    return None


def _patch_disable(text: str, context: ClassifierContext | None,
                   home: str | None, cwd: str | None) -> Match | None:
    if context is None:
        return None
    sections = re.split(r'^\*\*\* (Add File|Update File|Delete File|Move to): (.+)\n', text, flags=re.M)
    for i in range(1, len(sections), 3):
        operation, raw, body = sections[i:i + 3]
        target = _target(raw.strip(), context, home, cwd)
        if target is None or _hook_target(target, context) or target in context.kit_hook_files:
            return Match('hook-disable')
        if target in context.kit_entry_files and operation != 'Add File':
            return Match('hook-disable')
        added = '\n'.join(line[1:] for line in body.splitlines() if line.startswith('+'))
        if target in context.git_config_paths and _config_write(added, True):
            return Match('hook-disable')
        if target in context.host_config_paths and _host_off(added, target, True):
            return Match('hook-disable')
    return None


def _command_disable(words: list[str], context: ClassifierContext | None,
                     home: str | None, cwd: str | None) -> Match | None:
    if context is None or not words:
        return None
    program, args = os.path.basename(words[0]), words[1:]
    if program == 'adt' and args[:2] == ['hooks', 'remove'] and context.hooks_dir:
        return Match('hook-disable')
    # Classify only actual writes: operand mentions in readers are inert.
    targets = []
    if program in ('rm', 'mv', 'unlink', 'truncate', 'ln'):
        targets.extend(word for word in args if not word.startswith('-'))
    elif program == 'cp':
        operands = [word for word in args if not word.startswith('-')]
        if len(operands) == 2 and operands[0] == '/dev/null':
            targets.append(operands[1])
    elif program == 'chmod' and args:
        mode = args[0]
        symbolic_loss = re.search(r'(?:^|,)[ugoa]*-[rwxXst]*[xX]', mode)
        numeric_loss = re.fullmatch(r'[0-7]{3,4}', mode) and any(int(d) % 2 == 0 for d in mode[-3:])
        if symbolic_loss or numeric_loss:
            targets.extend(word for word in args[1:] if not word.startswith('-'))
    redirections = []
    for index, word in enumerate(words[:-1]):
        if word in ('>', '>>', '>|', '<>'):
            redirections.append(words[index + 1])
    targets.extend(redirections)
    for raw in targets:
        target = _target(raw, context, home, cwd)
        if target is None or _hook_target(target, context) or target in context.kit_hook_files or target in context.kit_entry_files:
            return Match('hook-disable')
        if raw in redirections:
            # printf's quoted payload stays one token; turn its escaped lines
            # into text for the config-key check without executing anything.
            text = ' '.join(words).replace('\\n', '\n').replace('\\t', '\t')
            if target in context.git_config_paths and _config_write(text, True):
                return Match('hook-disable')
            if target in context.host_config_paths and _host_off(text, target, True):
                return Match('hook-disable')
    return None


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


def _launch_page_match(value: str, bases: Sequence[str], home: str | None,
                       cwd: str | None, content_reader: bool = False) -> Match | None:
    if not value or not bases:
        return None
    candidate = _expand_home(value, home)
    if not os.path.isabs(candidate):
        if cwd is None:
            return None
        candidate = os.path.join(cwd, candidate)
    candidate = os.path.normpath(candidate)
    path = candidate.casefold()
    parent, leaf = os.path.split(path)
    base, folder = os.path.split(parent)
    for supplied in bases:
        runtime_base = os.path.normpath(supplied).casefold()
        page = leaf == LAUNCH_PAGE and base == runtime_base and _RUNTIME_ID.fullmatch(folder)
        directory = content_reader and (
            path == runtime_base or (parent == runtime_base and _RUNTIME_ID.fullmatch(leaf))
        )
        if page or directory:
            return Match("key-file-read", path=candidate)
    return None


def _prints_console_address(args: Sequence[str]) -> bool:
    """Find a print switch among open options, consuming known option values."""
    options = iter(args)
    for token in options:
        if token == "--":
            return False
        name, separator, _ = token.partition("=")
        if name in _CONSOLE_VALUE_OPTIONS:
            if not separator:
                next(options, None)
        elif token in _CONSOLE_PRINT_OPTIONS:
            return True
    return False


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
    elif re.search(r"(?<![\w./-])(?:/[^\s;|&'\"]*/)?git(?=[\s'\"]|$).*\bpush\b", text, re.S):
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
             inherited: Sequence[str], cwd: str | None = None,
             context: ClassifierContext | None = None, home: str | None = None,
             runtime_bases: Sequence[str] = ()) -> Match | None:
    original = words
    try:
        words, assignments, depth = _unwrap(words, depth)
    except ValueError:
        return _unparsed(" ".join(original), paths)
    configuration = [*inherited, *assignments]
    if not words:
        return None
    for word in words:
        match = _path_match(word, paths, cwd=cwd) or _launch_page_match(
            word, runtime_bases, home, cwd)
        if match:
            return match
    disabling = _command_disable(words, context, home, cwd)
    if disabling is not None:
        return disabling
    program = os.path.basename(words[0])
    args = words[1:]
    if program == "adt" and args[:2] == ["console", "open"] and _prints_console_address(args[2:]):
        return Match("operator-endpoint")
    if program == "eval":
        return _shell(" ".join(args), paths, depth + 1, configuration, cwd, context, home, runtime_bases)
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
                if index < len(args):
                    return _shell(args[index], paths, depth + 1, configuration, cwd,
                                  context, home, runtime_bases)
                return _unparsed(" ".join(words), paths)
        if "<<<" in args:
            index = args.index("<<<")
            if index + 1 < len(args):
                return _shell(args[index + 1], paths, depth + 1, configuration, cwd, context, home, runtime_bases)
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
        if operation == "config" and positional and _hook_config_key(positional[0]):
            read_only = any(name in options for name in ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"))
            if not read_only and (len(positional) > 1 or any(name in options for name in ("--unset", "--unset-all", "--remove-section"))):
                return Match("hook-disable")
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
            return _command(args, paths, depth + 1, configuration, cwd, context, home, runtime_bases)
        if args[:2] == ["-m", "relay_engine"]:
            return _command(["relay", *args[2:]], paths, depth + 1, configuration, cwd, context, home, runtime_bases)
    if program in ("relay", "relay.py"):
        _, args = _leading_options(args, {"--root": "root", "--key": "key"})
        if args[:1] in (["answer"], ["prompt-record"]) or args[:2] in (["authority", "prompt-record"], ["operator", "answer"], ["prompt", "record"]):
            return Match("operator-endpoint")
        if args[:2] in (["engine", "off"], ["team", "open"], ["team", "open-next"]):
            return Match("operator-only")
    if any([program, *args[:len(row) - 1]] == row
           for name in ("stop", "configure", "start", "update", "install", "checkout_register", "checkout_unregister", "unseat") for row in table()["operator_commands"][name]):
        return Match("operator-only")
    if any([program, *args[:len(row) - 1]] == row for row in table()["operator_commands"]["seat_claim"]):
        return Match("top-seat-init")
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


def _without_heredocs(text: str, bodies: dict[str, list[str]] | None = None) -> str:
    """Mask heredoc data without changing source offsets or shell syntax."""
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
                body = []
                while index < len(text):
                    end = text.find("\n", index)
                    end = len(text) if end < 0 else end + 1
                    raw_line = text[index:end]
                    line = raw_line.rstrip("\n")
                    index = end
                    output.append(''.join('\n' if char == '\n' else ' ' for char in raw_line))
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        if bodies is not None and not contexts:
                            bodies.setdefault(delimiter, []).append(''.join(body))
                        break
                    body.append(raw_line.lstrip("\t") if strip_tabs else raw_line)
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


def _shell_parts(text: str, source: str | None = None) -> tuple[str, list[tuple[int, str]]]:
    """Separate executable substitutions and discard non-executable comments."""
    source = text if source is None else source
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
            substitutions.append((sum(map(len, cleaned)), source[index + 2:end]))
            cleaned.append("__adt_substitution__")
            index = end + 1
            continue
        elif char == "`":
            end = _backtick_end(text, index)
            substitutions.append((sum(map(len, cleaned)), source[index + 1:end]))
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
           inherited: Sequence[str] = (), cwd: str | None = None,
           context: ClassifierContext | None = None, home: str | None = None,
           runtime_bases: Sequence[str] = ()) -> Match | None:
    if depth > 10:
        return _unparsed(text, paths)
    try:
        # Heredoc bodies are data for parsing, but still name refused files.
        mentioned_key = _refused(text, paths)
        heredocs = {}
        cleaned, substitutions = _shell_parts(_without_heredocs(text, heredocs), source=text)
        lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=";&|(){}<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        # shlex normalizes token text and reads one character ahead. Retain
        # source boundaries excluding that unread lookahead, not token spelling.
        words = [(word, lexer.instream.tell() - len(lexer._pushback_chars)) for word in lexer]
    except ValueError:
        return _unparsed(text, paths)
    segment = []
    environment = list(inherited)
    directory = cwd
    piped = None
    matches = []
    next_substitution = 0
    for word, boundary in [*words, (";", len(cleaned))]:
        if all(char in ";&|(){}\n" for char in word):
            # Consume genuine source occurrences once at their owning command,
            # before its effect and after all earlier commands.
            while next_substitution < len(substitutions) and substitutions[next_substitution][0] < boundary:
                _, command = substitutions[next_substitution]
                next_substitution += 1
                match = _shell(command, paths, depth + 1, environment, directory, context, home, runtime_bases)
                if match is not None:
                    matches.append(match)
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
                match = _shell(piped, paths, depth + 1, environment, directory, context, home, runtime_bases)
            else:
                match = _command(segment, paths, depth, environment, directory, context, home, runtime_bases)
            # A patch contributes at this command's position, never before
            # earlier refused segments. Consume other heredocs as data too.
            try:
                command_words, _, _ = _unwrap(segment, depth)
            except ValueError:
                command_words = []
            for index, token in enumerate(segment[:-1]):
                if token == '<<':
                    delimiter = segment[index + 1]
                    if delimiter not in heredocs:
                        delimiter = delimiter.lstrip('-')
                    bodies = heredocs.get(delimiter, [])
                    if bodies:
                        body = bodies.pop(0)
                        if command_words and os.path.basename(command_words[0]) == 'apply_patch':
                            disabling = _patch_disable(body, context, home, directory)
                            if disabling is not None:
                                match = disabling
            key = _refused(' '.join(segment), paths)
            if key:
                matches.append(Match('key-file-read', path=key))
            elif match:
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
    if mentioned_key and not any(match.effect == 'key-file-read' for match in matches):
        # Preserve refused-path protection for heredoc bodies discarded as data.
        matches.append(Match('key-file-read', path=mentioned_key))
    # A no-seat-refused effect wins before any effect a no-seat session passes.
    for match in matches:
        if table()["effects"][match.effect]["no_seat"] == 'refuse':
            return match
    # Otherwise retain the established compound merge/push rule.
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
                  protected_refs: Mapping[str, Sequence[str]] | None = None,
                  context: ClassifierContext | None = None,
                  runtime_bases: Sequence[str] = ()) -> Match | None:
    """Classify with hook-supplied context only; do not read environment or files."""
    name = tool_name.lower().split("__")[-1]
    url = tool_input.get("url")
    if name == 'apply_patch':
        patch = tool_input.get('command', '')
        disabling = _patch_disable(patch, context, home, cwd) if isinstance(patch, str) else None
        if disabling is None and isinstance(url, str) and _BRIDGE_ADDRESS.search(url):
            return Match("operator-endpoint")
        return disabling
    disabling = _file_disable(name, tool_input, context, home, cwd)
    if disabling is not None:
        return disabling
    match = _mcp_match(name, tool_input, protected_refs)
    content_reader = ((name == "grep" and tool_input.get("output_mode") == "content")
                      or (name not in ("glob", "list_directory") and name != "grep"
                          and any(part in name for part in ("search", "read_directory"))))
    for field, value in tool_input.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                key = _path_match(item, refused_paths, home, cwd, content_reader)
                if key:
                    return key
                if (field in ("path", "file_path", "paths", "file_paths")
                        and name not in ("glob", "list_directory")):
                    page = _launch_page_match(item, runtime_bases, home, cwd, content_reader)
                    if page:
                        return page
    command = tool_input.get("command", tool_input.get("cmd"))
    bridge = any(isinstance(value, str) and _BRIDGE_ADDRESS.search(value)
                 for value in (command, url))
    if isinstance(command, str):
        match = _shell(_expand_home(command, home), refused_paths, cwd=cwd, context=context,
                       home=home, runtime_bases=runtime_bases)
    if bridge and (match is None or table()["effects"][match.effect]["no_seat"] == "pass"):
        return Match("operator-endpoint")
    return match
