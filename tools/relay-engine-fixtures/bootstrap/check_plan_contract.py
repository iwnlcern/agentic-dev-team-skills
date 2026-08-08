"""Mechanical plan-vs-CLI consistency gate (R1 of the r30 and r31 reviews).

Usage:
    check_plan_contract.py <plan.md> <bootstrap_resolve.py> \
        <hostile_legs.py> <test_bootstrap_tools.py> \
        <check_plan_contract.py> <test_plan_contract.py>

Exit 0 only when the plan's canonical pin sites, documented invocations,
contract signatures with a complete command-tag bijection, test censuses,
and self-asserted counts all match the committed artifacts. Placement is
the claim: each digest is validated at its own normative site (command 2,
command 5's install, the consumer preamble, the pinned-artifact table),
never by mere occurrence anywhere in the file. The single `PLAN_ID:` line
is lineage history and is exempt from the textual sweeps; every other
line is current normative text.

Run at every issuance before the PLAN relay files, and at Task 0 as
command 3 from the committed worktree copy.
"""
import ast
import hashlib
import importlib.util
import re
import sys

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
         7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
         12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
         16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
         20: "twenty", 21: "twenty-one", 22: "twenty-two",
         23: "twenty-three", 24: "twenty-four", 25: "twenty-five"}

# The five content-bearing census files whose digests command 2 pins in
# the worktree, keyed by their committed paths.
CENSUS_PINS = (
    ("tools/relay-engine-fixtures/bootstrap/hostile_legs.py", "runner"),
    ("tools/relay-engine-fixtures/bootstrap/bootstrap_resolve.py",
     "resolver"),
    ("tools/relay-engine-fixtures/bootstrap/check_plan_contract.py",
     "checker"),
    ("tools/relay_engine/tests/test_bootstrap_tools.py", "unittest"),
    ("tools/relay_engine/tests/test_plan_contract.py", "contract-tests"),
)

STALE_PHRASES = (
    "fenced program", "fenced Python program",
    "No commit is made by this task",
    "written byte-exactly from the fenced",
    # r31 R1: the carrier publication primitive is link-based no-replace;
    # the rename description is the stale r30 text.
    "fsync, rename, dirfd fsync",
    # r31 R2: classification may never route through the installed copy.
    "classification occurs before the fourteen-command sequence and"
    " defines `LB` by invoking that same `RESOLVE",
)


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_resolver(path):
    spec = importlib.util.spec_from_file_location("bootstrap_resolve", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def split_args(argtext):
    return re.findall(r"\"[^\"]*\"|'[^']*'|<[^>]+>|[^\s]+",
                      argtext.strip())


def test_methods(path, class_name):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return sorted(
        node.name
        for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
        and cls.name == class_name
        for node in cls.body if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_"))


def main(argv):
    if len(argv) != 7:
        sys.stderr.write(__doc__)
        return 2
    (plan_path, resolver_path, runner_path, test_path, checker_path,
     ctest_path) = argv[1:7]
    with open(plan_path, encoding="utf-8") as f:
        plan_full = f.read()
    # Lineage delimitation: exactly ONE PLAN_ID: line, at the canonical
    # top-of-document position before the first section heading, carries
    # revision history (old digests, old phrases) and is exempt; any
    # additional PLAN_ID:-prefixed line would smuggle normative text out
    # of the sweeps and is refused (R4 of the r32 review).
    plan_lines = plan_full.splitlines()
    pid_idx = [i for i, l in enumerate(plan_lines)
               if l.startswith("PLAN_ID:")]
    head_idx = next((i for i, l in enumerate(plan_lines)
                     if l.startswith("### ")), len(plan_lines))
    if len(pid_idx) != 1:
        sys.stderr.write("exactly one PLAN_ID lineage line is required,"
                         " found %d\n" % len(pid_idx))
        return 1
    if pid_idx[0] > head_idx:
        sys.stderr.write("the PLAN_ID lineage line must precede the"
                         " first section heading\n")
        return 1
    body = "\n".join(l for l in plan_lines if not l.startswith("PLAN_ID:"))
    mod = load_resolver(resolver_path)
    subs = mod.SUBCOMMANDS
    digests = {
        "runner": sha256_file(runner_path),
        "resolver": sha256_file(resolver_path),
        "unittest": sha256_file(test_path),
        "checker": sha256_file(checker_path),
        "contract-tests": sha256_file(ctest_path),
    }
    bad = []

    # 0. The fourteen numbered command lines, each exactly once at line
    # start INSIDE the Task 0 section (the state-table arms are indented,
    # and later tasks' numbered checklists sit outside this section).
    t0 = re.search(r"^### Task 0.*?(?=^### |\Z)", body, re.M | re.S)
    if not t0:
        bad.append("no '### Task 0' section found")
        task0 = ""
    else:
        task0 = t0.group(0)
    cmd_lines = {}
    for n in range(1, 15):
        hits = re.findall(r"^%d\. .*$" % n, task0, re.M)
        if len(hits) != 1:
            bad.append("command %d appears %d times at line start in"
                       " Task 0; the sequence needs exactly one"
                       % (n, len(hits)))
            continue
        cmd_lines[n] = hits[0]

    # 1. Canonical pin site: command 2 pins every content-bearing census
    # file's digest at its committed worktree path, each path exactly
    # once -- an executable equality chain admits no duplicate for the
    # same path, because every equality must hold (R2 of the r32
    # review: a wrong earlier pin must never be shadowed by a correct
    # later one), and no unexpected path may carry a pin.
    if 2 in cmd_lines:
        pins = re.findall(
            r"shasum -a 256 (\S+) \| cut -d' ' -f1\)\" = "
            r"\"([0-9a-f]{64})\"", cmd_lines[2])
        expected = {path: label for path, label in CENSUS_PINS}
        for path, label in CENSUS_PINS:
            hexes = [h for p, h in pins if p == path]
            if not hexes:
                bad.append("command 2 does not pin %s at %s"
                           % (label, path))
            elif len(hexes) > 1:
                bad.append("command 2 pins %s %d times; the equality"
                           " chain admits exactly one pin per path"
                           % (label, len(hexes)))
            elif hexes[0] != digests[label]:
                bad.append("command 2 pins %s as %s, the committed file"
                           " is %s" % (label, hexes[0], digests[label]))
        for p, _ in pins:
            if p not in expected:
                bad.append("command 2 pins unexpected path %s" % p)

    # 2. Canonical pin site: command 3 runs the committed checker copy.
    if 3 in cmd_lines and \
            "tools/relay-engine-fixtures/bootstrap/check_plan_contract.py" \
            not in cmd_lines[3]:
        bad.append("command 3 does not run the committed checker copy")

    # 3. Canonical pin site: command 5 binds each digest to the installed
    # PATH being hashed, never to an ordered digest list (R3 of the r32
    # review: hashing the wrong installed file with the right literals
    # in the right order must be red).
    if 5 in cmd_lines:
        c5 = cmd_lines[5]
        rm5 = re.search(r'RSHA="\$\(shasum -a 256 "([^"]+)" \| cut', c5)
        if not rm5 or rm5.group(1) != \
                "$RELAY_ENGINE_RESULTS_ROOT/v29-engine-hostile-legs.py":
            bad.append("command 5's RSHA must hash exactly"
                       " $RELAY_ENGINE_RESULTS_ROOT/"
                       "v29-engine-hostile-legs.py; a suffix match would"
                       " accept an attacker-selected root")
        rq5 = re.search(r'\[ "\$RSHA" = "([0-9a-f]{64})" \]', c5)
        if not rq5 or rq5.group(1) != digests["runner"]:
            bad.append("command 5 does not bind the installed runner"
                       " path to the committed runner digest")
        vq5 = re.search(r'\[ "\$\(shasum -a 256 "\$RESOLVE" \|'
                        r" cut -d' ' -f1\)\" = \"([0-9a-f]{64})\" \]", c5)
        if not vq5 or vq5.group(1) != digests["resolver"]:
            bad.append("command 5 does not bind the installed resolver"
                       " path to the committed resolver digest")
        if len(re.findall(r"= \"([0-9a-f]{64})\"", c5)) != 2:
            bad.append("command 5 must carry exactly the two"
                       " path-bound digest equalities")
    # The $RESOLVE indirection is bound at its one canonical assignment
    # site, not by section-wide occurrence (R1 of the r33 review: a
    # displaced wrong assignment plus a correct decoy elsewhere must be
    # red): Task 0 carries exactly ONE RESOLVE= assignment, its value is
    # the installed resolver path, and it precedes command 1.
    want_resolve = ("$RELAY_ENGINE_RESULTS_ROOT/"
                    "v29-engine-bootstrap-resolve.py")
    rdefs = re.findall(r'\bRESOLVE="([^"]*)"', task0)
    if rdefs != [want_resolve]:
        bad.append("Task 0 must bind RESOLVE exactly once to %s;"
                   " found %r" % (want_resolve, rdefs))
    elif 1 in cmd_lines and \
            task0.find('RESOLVE="') > task0.find(cmd_lines[1]):
        bad.append("the RESOLVE binding must precede command 1 in the"
                   " execution preamble")

    # 4. Canonical pin site: the consumer preamble byte-pins the resolver
    # and passes the runner digest to resolve.
    pm = re.search(r"\*\*Consumer preamble.*?(?=\n\n|\Z)", body, re.S)
    if not pm:
        bad.append("no consumer-preamble paragraph found")
    else:
        pre = pm.group(0)
        rm = re.search(r"bootstrap-resolve\.py' \| cut -d' ' -f1\)\" = "
                       r"'([0-9a-f]{64})'", pre)
        if not rm or rm.group(1) != digests["resolver"]:
            bad.append("consumer preamble does not pin the committed"
                       " resolver digest")
        km = re.search(r"resolve '[^']*' '([0-9a-f]{64})'", pre)
        if not km or km.group(1) != digests["runner"]:
            bad.append("consumer preamble does not pass the committed"
                       " runner digest to resolve")

    # 5. Canonical pin site: the pinned-artifact table names each
    # committed file with its current digest.
    tm = re.search(r"\*\*The pinned machinery artifacts.*?(?=\n\n|\Z)",
                   body, re.S)
    if not tm:
        bad.append("no pinned-artifact table found")
    else:
        table = tm.group(0)
        for base, label in (("hostile_legs.py", "runner"),
                            ("bootstrap_resolve.py", "resolver"),
                            ("test_bootstrap_tools.py", "unittest"),
                            ("check_plan_contract.py", "checker"),
                            ("test_plan_contract.py", "contract-tests")):
            am = re.search(r"`%s`[^`]*SHA-256 `([0-9a-f]{64})`"
                           % re.escape(base), table)
            if not am:
                bad.append("artifact table does not pin %s" % base)
            elif am.group(1) != digests[label]:
                bad.append("artifact table pins %s as %s, the committed"
                           " file is %s" % (base, am.group(1),
                                            digests[label]))

    # 6. Every documented `"$RESOLVE" <sub> ...` invocation matches the
    # committed arity, and the numbered lines yield the command map.
    inv = re.compile(r'"\$RESOLVE"[ \t]+([a-z-]+)'
                     r'((?:[ \t]+(?:"[^"]*"|\'[^\']*\'|<[^>]+>|'
                     r'[^\s`|&;)]+))*)')
    hits = 0
    for m in inv.finditer(body):
        sub, argtext = m.group(1), m.group(2)
        if sub not in subs:
            bad.append("documented subcommand %r is not in the CLI" % sub)
            continue
        hits += 1
        n = len(split_args(argtext))
        if n != subs[sub]:
            bad.append("invocation of %s documents %d args, CLI takes %d:"
                       " %r" % (sub, n, subs[sub], m.group(0)[:120]))
    if hits == 0:
        bad.append("no \"$RESOLVE\" invocations found in the plan")
    cmd_map = {}
    for n, line in cmd_lines.items():
        found = set(re.findall(r'"\$RESOLVE"\s+([a-z-]+)', line))
        if found:
            cmd_map[n] = found

    # 7. Consumer-preamble invocations obey the same arity rule.
    pre_re = re.compile(r"bootstrap-resolve\.py'[ \t]+([a-z-]+)"
                        r"((?:[ \t]+'[^']*')*)")
    for m in pre_re.finditer(body):
        sub, argtext = m.group(1), m.group(2)
        if sub not in subs:
            bad.append("preamble subcommand %r is not in the CLI" % sub)
            continue
        n = len(split_args(argtext))
        if n != subs[sub]:
            bad.append("preamble %s documents %d args, CLI takes %d"
                       % (sub, n, subs[sub]))

    # 8. Contract signatures: arity, and a COMPLETE command-tag
    # bijection -- every sequence-used subcommand's signature carries
    # exactly the command numbers that run it, and every tag points at a
    # command that runs its subcommand (r31 R1: an optional tag proves
    # nothing).
    sig = re.compile(r"`([a-z-]+)((?: <[^>]+>)+)`((?: \(command \d+\))*)")
    sigs_seen = set()
    sig_tags = {}
    for m in sig.finditer(body):
        sub = m.group(1)
        if sub not in subs:
            continue
        sigs_seen.add(sub)
        n = len(re.findall(r"<[^>]+>", m.group(2)))
        if n != subs[sub]:
            bad.append("contract signature for %s documents %d args,"
                       " CLI takes %d" % (sub, n, subs[sub]))
        for t in re.findall(r"\(command (\d+)\)", m.group(3)):
            sig_tags.setdefault(sub, set()).add(int(t))
    for sub in subs:
        if sub not in sigs_seen:
            bad.append("no contract signature documents %s" % sub)
    used_by = {}
    for n, found in cmd_map.items():
        for sub in found:
            used_by.setdefault(sub, set()).add(n)
    for sub in subs:
        want = used_by.get(sub, set())
        got = sig_tags.get(sub, set())
        if want != got:
            bad.append("command-tag bijection broken for %s: the sequence"
                       " runs it in %s, its signature is tagged %s"
                       % (sub, sorted(want), sorted(got)))

    # 9. Test censuses: every listed census matches the committed
    # methods, count word included.
    for cls, path in (("TestBootstrapTools", test_path),
                      ("TestPlanContract", ctest_path)):
        methods = test_methods(path, cls)
        census = re.findall(r"%s` \(([^)]*)\)" % cls, body)
        if not census:
            bad.append("no %s census found in the plan" % cls)
        for inner in census:
            cm = re.match(r"(\S+) tests: (.*)", inner, re.S)
            if not cm:
                bad.append("census %r does not read '<count> tests: ...'"
                           % inner[:60])
                continue
            if cm.group(1) != WORDS.get(len(methods)):
                bad.append("%s census counts %r tests, the committed file"
                           " has %s" % (cls, cm.group(1),
                                        WORDS.get(len(methods))))
            listed = sorted(re.findall(r"`(test_[a-z0-9_]+)`",
                                       cm.group(2)))
            if listed != methods:
                bad.append("%s census names %s but the committed file"
                           " has %s" % (cls, listed, methods))

    # 10. Self-asserted counts: resolve triples and record classes.
    want = WORDS.get(mod.RESOLVE_TRIPLES)
    for word in re.findall(r"([a-z-]+) resolve triples", body):
        if word != want:
            bad.append("plan claims %r resolve triples, the resolver"
                       " self-asserts %r" % (word, want))
    want = WORDS.get(mod.RECORD_CLASSES)
    low = body.lower()
    for word in re.findall(r"([a-z-]+) (?:framed )?record classes", low):
        if word not in ("the", "all") and word != want:
            bad.append("plan claims %r record classes, the resolver"
                       " self-asserts %r" % (word, want))
    # Every OTHER normative class-count spelling about the record/framing
    # machinery is swept too (R1 of the r32 review: `six classes`,
    # `six-class`, and `all six classes` were invisible to the exact
    # `record classes` shape): any number-word directly compounded with
    # `class`/`classes` whose surrounding text is about records, framing,
    # or the truncation fixtures must carry the current count. Class
    # counts of unrelated features (reader outputs, gates) are untouched.
    numwords = "|".join(sorted(WORDS.values(), key=len, reverse=True))
    for m in re.finditer(r"\b(%s)[- ]class(?:es)?\b" % numwords, low):
        window = low[max(0, m.start() - 120):m.end() + 120]
        if any(t in window for t in ("record", "framed", "framing",
                                     "frame", "truncat")):
            if m.group(1) != want:
                bad.append("plan claims %r record-machinery classes,"
                           " the resolver self-asserts %r"
                           % (m.group(1), want))

    # 11. Stale-contract phrases from abolished revisions must be gone
    # from the normative body.
    for phrase in STALE_PHRASES:
        if phrase in body:
            bad.append("stale phrase still present: %r" % phrase)

    # 12. Issuance neutrality (R1 of the r34 review): the plan may never
    # hard-code a plan/review handoff id as its live authority pair --
    # such a pair is consumed by its own review and goes stale inside a
    # document that outlives it. The live pair is defined referentially
    # by the single PLAN relay that issues this document; any
    # `v29-engine-plan...` id in the normative body is red (the exempt
    # PLAN_ID lineage line may carry history; `v29-engine-impl` and
    # other non-plan handoff ids are unaffected).
    for m in set(re.findall(r"v29-engine-plan[a-z0-9-]*", body)):
        bad.append("hard-coded handoff id %r in the normative body;"
                   " the live pair is defined by the issuing relay" % m)

    # 13. Option B (PLAN-orchestrator-planner-20260808-142107.md): the
    # revision-stable plan holds no standing authority to destroy
    # committed work when its own pins move. No destructive discard op
    # may appear as an executable instruction anywhere in the normative
    # body -- the one-time discard-and-rebind of a superseded pre-binding
    # commit is issued in the single-use dispatch, archive-before-discard
    # and bound to the exact incident.
    #
    # The recogniser evolved across two review rounds, and both defects
    # were the same mistake made at different granularities -- assuming a
    # destructive operation has one canonical spelling. R1 of the r41
    # review: raw substring search for `git reset --hard`/`git branch
    # -D`/`git worktree remove` missed `git -C "$PWD" reset --hard` (a git
    # global option before the subcommand), `git reset  --hard` (doubled
    # whitespace), and `git update-ref -d ...` (an equivalent ref
    # deletion). R1 of the r42 review: even whitespace-normalised ADJACENT
    # substrings (`reset --hard`, `branch --delete`, `update-ref -d`)
    # missed a valid option INTERPOSED between the subcommand and its
    # destructive flag -- `git reset --quiet --hard`, `git branch --force
    # --delete`, `git update-ref --no-deref -d` each returned exit 0.
    #
    # The recogniser is membership-based over SHELL-ARGV-NORMALISED text,
    # not positional or textual. R1 of the r43 review showed raw whitespace
    # tokens missed quoted flags/refs, absolute-path git, and shell
    # wrappers; R1 of the r44 review showed that DELETING/spacing quotes is
    # still only a textual approximation -- the shell also joins adjacent
    # quoted/unquoted fragments, removes backslash escapes and backslash-
    # newline continuations, and can produce the executable word from a
    # command substitution. `git re'set'`, `g''it reset`, `git reset
    # --har\d`, a `\`-newline-continued `git reset` / `--hard`, and
    # `$(command -v git) reset --hard` each returned exit 0. The normaliser
    # below reconstructs argv-relevant text the way the shell does, in
    # order, BEFORE tokenising:
    #   1. join backslash-newline line continuations;
    #   2. remove backslash escapes (`\X` -> `X`), so `--har\d` -> `--hard`;
    #   3. delete quote characters WITHOUT inserting a boundary, so adjacent
    #      fragments join as the shell joins them (`re'set'` -> `reset`,
    #      `g''it` -> `git`), which also flattens shell-wrapper command
    #      strings (`sh -c 'git reset --hard'`) into the same segment;
    #   4. blank out `$`, `(`, `)`, and backtick so a command substitution
    #      exposes its executable-producing inner tokens in the segment
    #      (`$(command -v git)` -> `command -v git`).
    # Then split into command segments on shell separators (newline, `;`,
    # `|`, `&`), recognise a Git invocation by executable BASENAME (`git`,
    # `/usr/bin/git`, `./git`), and flag a destructive operation when its
    # subcommand token AND one of its destructive flag/ref tokens are both
    # present in the segment -- independent of quoting, escapes, line
    # continuation, executable path/substitution, option order, and
    # whitespace. `worktree` is destructive only paired with `remove` (so
    # `git worktree add`/`list` stay green); `update-ref` paired with a
    # delete/stdin flag or the literal target ref.
    #
    # CONTRACT (Option 1, ruled PLAN-orchestrator-planner-20260808-160930.md):
    # check 13 GUARDS AGAINST REGRESSION, NOT EVASION. A regression is an
    # author who forgot the rule -- which is literally what r40 did, in good
    # faith, and what this gate exists to catch. An evasion is an author who
    # knows the rule and disguises the op; that is out of scope, because the
    # plan under review would itself be the attack -- an authoring seat
    # hostile enough to encode `reset` past this gate has already defeated
    # its Implementer's review, this gate, and the human reading the plan.
    #
    # This is a presence check (deletion-proof like the r40 inversion) and a
    # good-faith regression DETECTOR, NOT a no-destructive-command proof. It
    # is deliberately not worded as closure (r36 lesson: a mechanism that
    # narrows a class without closing it must say so). It catches the
    # destructive op written in any of these ten ordinary syntax classes:
    #   1. plain (`git reset --hard`);
    #   2. a git global option before the subcommand (`git -C x reset --hard`);
    #   3. an option interposed before the destructive flag (`git reset
    #      --quiet --hard`);
    #   4. a quoted flag/ref (`git reset '--hard'`);
    #   5. adjacent-fragment concatenation (`git re'set'`, `g''it`);
    #   6. a backslash escape (`git reset --har\d`);
    #   7. backslash-newline line continuation;
    #   8. an absolute/relative executable path (`/usr/bin/git`, `./git`);
    #   9. an executable-producing command substitution (`$(command -v git)`);
    #  10. a shell-wrapper command string (`sh -c 'git reset --hard'`).
    #
    # RESIDUAL, named specifically and documented executably (see
    # test_documents_static_obfuscation_residual, which asserts the checker
    # returns exit 0 on each -- a control that documents the gap rather than
    # asserting it safe, and fails loudly if a later revision ever closes
    # one). The gate does NOT catch these deliberately self-obfuscating
    # forms, all of which require the authoring seat itself to be hostile:
    #   * alias delegation -- `git -c alias.wipe='reset --hard' wipe`;
    #   * brace expansion -- `git {reset,--hard} HEAD~1`;
    #   * ANSI-C quoting -- `git $'\x72\x65\x73\x65\x74' --hard`;
    #   * and, more generally, any destructiveness produced only at RUN time
    #     by data the checker cannot evaluate -- shell variable indirection
    #     (`c=reset; git $c --hard`), `eval` of a computed string, or a
    #     decoded/piped program -- which is undecidable for any static gate.
    # This residual is weaker than the operator-accepted fault-leg same-UID
    # residual (that admitted a real runtime actor; this requires the plan
    # author itself to be hostile), and is out of Option B's process threat
    # model. The exempt `PLAN_ID:` lineage line may name these ops in prose;
    # the normative body may not carry them as good-faith instructions.
    target = "refs/heads/feat/v29-engine"
    DESTRUCTIVE_OPS = {
        "reset": {"--hard"},                       # reset over committed work
        "branch": {"-D", "-d", "--delete"},        # branch deletion (any force)
        "worktree": {"remove"},                    # worktree teardown
        "update-ref": {"-d", "--stdin", target},   # ref deletion/movement
    }
    norm = body.replace("\\\n", "")            # 1. line continuations
    norm = re.sub(r"\\(.)", r"\1", norm)       # 2. backslash escapes
    norm = norm.replace("'", "").replace('"', "")   # 3. join quoted fragments
    norm = re.sub(r"[$()`]", " ", norm)        # 4. expose substitution execs
    def _invokes_git(tokens):
        for t in tokens:
            if t.rsplit("/", 1)[-1] == "git":
                return True
        return False
    for seg in re.split(r"[\n;|&]+", norm):
        toks = seg.split()
        if not _invokes_git(toks):
            continue
        tokset = set(toks)
        for sub, flags in DESTRUCTIVE_OPS.items():
            hits = flags & tokset
            if sub in tokset and hits:
                bad.append("Option B forbids the destructive op 'git %s"
                           " ... %s' in the revision-stable plan"
                           " (recognised by subcommand+flag membership over"
                           " shell-argv-normalised text -- quoting, escapes,"
                           " continuation, executable path/substitution, and"
                           " order independent); the discard-and-rebind"
                           " recovery belongs in the single-use dispatch"
                           % (sub, sorted(hits)[0]))

    if bad:
        sys.stderr.write("\n".join(bad) + "\n")
        return 1
    sys.stdout.write(
        "plan-contract-ok subcommands=%d census=%d contract-tests=%d\n"
        % (len(subs), len(test_methods(test_path, "TestBootstrapTools")),
           len(test_methods(ctest_path, "TestPlanContract"))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
