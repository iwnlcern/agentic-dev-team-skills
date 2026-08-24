import errno, hashlib, os, re, shutil, stat, subprocess, sys, tempfile

TARGET_BRANCH = "feat/v29-engine"
RESOLVE_TRIPLES = 23  # entries in the self-test `cases` table, self-asserted
# Scan-tuple positions of the framed record classes (bootstrap, supersede,
# pass, invocation, exit, binding, rebase-witness); fault-leg sweeps these
# and the plan's "<word> record classes" phrases cross-check the count.
CLASS_INDEXES = (0, 2, 3, 4, 5, 6, 7)
RECORD_CLASSES = len(CLASS_INDEXES)
# The D31 machinery-commit census: command 1 commits exactly these paths,
# and INITIAL-BIND's interrupted arm re-verifies HEAD against this list.
MACHINERY_CENSUS = (
    "tools/relay-engine-fixtures/bootstrap/bootstrap_resolve.py",
    "tools/relay-engine-fixtures/bootstrap/check_plan_contract.py",
    "tools/relay-engine-fixtures/bootstrap/hostile_legs.py",
    "tools/relay_engine/__init__.py",
    "tools/relay_engine/tests/__init__.py",
    "tools/relay_engine/tests/test_bootstrap_tools.py",
    "tools/relay_engine/tests/test_plan_contract.py",
)
REC = re.compile(r"rec (\d+) ([0-9a-f]{64}) (.*)", re.S)
B = re.compile(r"bootstrap (\d+) sha256=([0-9a-f]{64}) \| (.*)")
S = re.compile(r"supersede bootstrap (\d+)")
P = re.compile(r"hostile-legs-pass (\d+) payload-sha256=([0-9a-f]{64})"
               r" runner-sha256=([0-9a-f]{64})")
BIND = re.compile(r"binding branch=feat/v29-engine"
                  r" bound_sha=([0-9a-f]{40}) branch_tip=([0-9a-f]{40})"
                  r" previous_bound_sha=([0-9a-f]{40}|none)"
                  r" producer=(task0|rebind)")
WITNESS = re.compile(r"rebase-witness from=([0-9a-f]{40}) onto=([0-9a-f]{40})"
                     r" old_tip=([0-9a-f]{40})"
                     r" patch_ids=((?:[0-9a-f]{40})(?:,[0-9a-f]{40})*|none)")
INV = re.compile(r"hostile-legs-invocation n=(\d+) runner-sha256=([0-9a-f]{64})")
EXITR = re.compile(r"hostile-legs-exit n=(\d+) status=(\d+)")
ENV_KEYS = ("COORD_ROOT", "RELAY_ENGINE_RESULTS_ROOT", "PYTHON",
            "DETECTION_REF", "DETECTION_SHA")
BOOT_TMPL = ("ENGINE_ENV='{env}' && [ -f \"$ENGINE_ENV\" ] && "
             "[ ! -L \"$ENGINE_ENV\" ] && source \"$ENGINE_ENV\" && "
             "[ \"$COORD_ROOT\" = '{c}' ] && "
             "[ \"$RELAY_ENGINE_RESULTS_ROOT\" = '{r}' ] && "
             "[ \"$PYTHON\" = '{p}' ] && [ \"$DETECTION_REF\" = '{ref}' ] && "
             "[ \"$DETECTION_SHA\" = '{sha}' ] && "
             "\"$PYTHON\" -c 'import sys; assert sys.version_info >= (3,11)'")

def valid_basename(name):
    return (name not in ("", ".", "..") and "/" not in name
            and "\x00" not in name)

def body_hex(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

def frame(body):
    return "rec %d %s %s" % (len(body.encode("utf-8")), body_hex(body), body)

def parse_line(line):
    m = REC.fullmatch(line)
    if not m:
        return None
    body = m.group(3)
    if len(body.encode("utf-8")) != int(m.group(1)):
        return None
    if body_hex(body) != m.group(2):
        return None
    return body

def write_all(fd, buf):
    done = os.write(fd, buf)
    if done != len(buf):
        raise OSError("short write: %d of %d bytes" % (done, len(buf)))
    os.fsync(fd)

def open_confined(path, flags, mode=0o644):
    # Every proof reader and writer: dirfd + exact-basename O_NOFOLLOW,
    # O_NONBLOCK so a FIFO can never block before its refusal, and a
    # regular-file fstat check on the held descriptor before any use.
    dirname, base = os.path.split(path)
    if not valid_basename(base):
        raise OSError("proof basename fails the basename rule")
    dfd = os.open(dirname or ".", os.O_RDONLY | os.O_DIRECTORY
                  | os.O_NOFOLLOW)
    try:
        fd = os.open(base, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode,
                     dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("proof path is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd

def open_append(path):
    return open_confined(path, os.O_RDWR | os.O_APPEND | os.O_CREAT)

def terminate(path):
    fd = open_append(path)
    try:
        data, torn = drop_torn(read_fd_bytes(fd))
        if torn:
            os.ftruncate(fd, len(data))
            os.fsync(fd)
    finally:
        os.close(fd)

def append_record(path, body):
    if "\n" in body or "\r" in body:
        raise OSError("record body may not contain line separators")
    terminate(path)
    fd = open_append(path)
    try:
        write_all(fd, (frame(body) + "\n").encode("utf-8"))
    finally:
        os.close(fd)

def drop_torn(data):
    torn = len(data) > 0 and not data.endswith(b"\n")
    if torn:
        data = data[:data.rfind(b"\n") + 1]
    return data, torn

def read_fd_bytes(fd):
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)

def read_proof_bytes(path):
    fd = open_confined(path, os.O_RDONLY)
    try:
        return read_fd_bytes(fd)
    finally:
        os.close(fd)

def complete_lines(path):
    data, torn = drop_torn(read_proof_bytes(path))
    return data.decode("utf-8", "replace").split("\n")[:-1], torn

def scan(lines):
    boots, payloads, sups = {}, {}, set()
    passes, binds, invs, exits, wits = [], [], [], [], []
    for lineno, ln in enumerate(lines, 1):
        body = parse_line(ln)
        if body is None:
            if ln.startswith("rec "):
                return None, "corrupt frame at line %d" % lineno
            continue
        m = B.fullmatch(body)
        if m:
            k, hx, payload = int(m.group(1)), m.group(2), m.group(3)
            if k in boots:
                return None, "duplicate bootstrap number %d" % k
            if body_hex(payload) != hx:
                return None, "bootstrap %d payload digest mismatch" % k
            boots[k] = hx
            payloads[k] = payload
            continue
        m = S.fullmatch(body)
        if m:
            k = int(m.group(1))
            if k in sups:
                return None, "duplicate supersession of bootstrap %d" % k
            sups.add(k)
            continue
        m = P.fullmatch(body)
        if m:
            passes.append((int(m.group(1)), m.group(2), m.group(3)))
            continue
        m = INV.fullmatch(body)
        if m:
            invs.append(m.groups())
            continue
        m = EXITR.fullmatch(body)
        if m:
            exits.append(m.groups())
            continue
        if body.startswith("binding "):
            m = BIND.fullmatch(body)
            if not m:
                return None, "malformed binding record"
            binds.append(m.groups())
            continue
        if body.startswith("rebase-witness "):
            m = WITNESS.fullmatch(body)
            if not m:
                return None, "malformed rebase-witness record"
            wits.append(m.groups())
    return (boots, payloads, sups, passes, binds, invs, exits, wits), ""

def scan_current(path):
    lines, _ = complete_lines(path)
    return scan(lines)

def git_out(worktree, *args):
    out = subprocess.run(["git", "-C", worktree] + list(args),
                         capture_output=True, text=True)
    return out.returncode, out.stdout.strip()

def git_truth(worktree, bound_sha, recorded_tip):
    # The single Git-truth predicate, shared by resolve and classify: the
    # worktree is on the literal branch, the bound sha is an ancestor of
    # the current tip, and the recorded tip is the current tip or its
    # ancestor. Returns (ok, message).
    rc, branch = git_out(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch != TARGET_BRANCH:
        return False, "worktree is not on %s" % TARGET_BRANCH
    rc, tip = git_out(worktree, "rev-parse", "HEAD")
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", tip):
        return False, "worktree tip is unreadable"
    if git_out(worktree, "merge-base", "--is-ancestor", bound_sha,
               tip)[0] != 0:
        return False, "bound sha is not an ancestor of the worktree tip"
    if tip != recorded_tip and git_out(
            worktree, "merge-base", "--is-ancestor", recorded_tip,
            tip)[0] != 0:
        return (False, "recorded branch_tip is neither the worktree tip"
                " nor its ancestor")
    return True, "git-truth-ok"

def patch_ids(worktree, rng):
    # Rebase-stable identity of the commits in rng (e.g. "A..B"): the
    # sorted set of git patch-ids, invariant across a `rebase --onto`.
    rc, out = git_out(worktree, "rev-list", rng)
    if rc != 0:
        return None
    ids = []
    for commit in out.split():
        dt = subprocess.run(["git", "-C", worktree, "diff-tree", "-p",
                             commit], capture_output=True)
        pid = subprocess.run(["git", "-C", worktree, "patch-id", "--stable"],
                             input=dt.stdout, capture_output=True)
        parts = pid.stdout.decode("utf-8", "replace").split()
        if parts:
            ids.append(parts[0])
    return tuple(sorted(ids))

def resolve(path, runner_hex, expected_sha, worktree=None):
    lines, torn = complete_lines(path)
    scanned, err = scan(lines)
    if scanned is None:
        return 1, err, torn
    boots, payloads, sups, passes, binds = scanned[:5]
    if not boots:
        return 1, "no bootstrap record", torn
    if sorted(boots) != list(range(1, len(boots) + 1)):
        return 1, "bootstrap numbers are not exactly 1..k", torn
    for k in sups:
        if k not in boots:
            return 1, "supersession names absent bootstrap %d" % k, torn
    current = [k for k in boots if k not in sups]
    if len(current) == 0:
        return (1, "zero unsuperseded records: interrupted rebind;"
                " rerun the record-placement command", torn)
    if len(current) != 1:
        return 1, "%d unsuperseded records" % len(current), torn
    n = current[0]
    if expected_sha not in payloads[n]:
        return (1, "current payload does not carry the expected"
                " DETECTION_SHA", torn)
    ok = [p for p in passes if p == (n, boots[n], runner_hex)]
    if len(ok) != 1:
        return (1, "expected exactly one valid hostile-legs-pass for"
                " bootstrap %d, found %d" % (n, len(ok)), torn)
    if not binds:
        return 1, "no binding block for the current bootstrap", torn
    if binds[-1][0] != expected_sha:
        return (1, "latest binding does not bind the expected"
                " DETECTION_SHA", torn)
    if worktree is not None:
        # The durable verdict relates the binding to Git truth -- a reset,
        # recreated, or moved branch is RED however green the records are.
        ok, msg = git_truth(worktree, expected_sha, binds[-1][1])
        if not ok:
            return 1, msg, torn
    return 0, "current=%d payload-sha256=%s" % (n, boots[n]), torn

def open_root(root):
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

def carrier_name(n):
    return "v29-engine-bootstrap-line-%d.txt" % int(n)

def read_exact(root_fd, name, limit):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=root_fd)
    except OSError as e:
        return None, "open refused: %s" % e.strerror
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file: %s" % name
        if st.st_size == 0 or st.st_size > limit:
            return None, "empty or oversized: %s" % name
        return read_fd_bytes(fd), ""
    finally:
        os.close(fd)

def clean_value(v):
    return "'" not in v and all(0x20 <= ord(ch) != 0x7f for ch in v)

def read_env(root_fd, env_basename):
    data, err = read_exact(root_fd, env_basename, 8192)
    if data is None:
        return None, err
    vals = {}
    for ln in data.decode("utf-8").splitlines():
        m = re.fullmatch(r"export ([A-Z_]+)='([^']*)'", ln)
        if not m or m.group(1) not in ENV_KEYS:
            return None, "unrecognized env line"
        if m.group(1) in vals:
            return None, "duplicate env key %s" % m.group(1)
        if not clean_value(m.group(2)) or not m.group(2):
            return None, "env value fails the character rule"
        vals[m.group(1)] = m.group(2)
    if sorted(vals) != sorted(ENV_KEYS):
        return None, "env file must hold exactly the five exports"
    return vals, ""

def routed_env(root_fd, root, env_basename, expected_ref, expected_sha):
    vals, err = read_env(root_fd, env_basename)
    if vals is None:
        return None, err
    if vals["DETECTION_REF"] != expected_ref:
        return None, "env DETECTION_REF does not match the routed value"
    if vals["DETECTION_SHA"] != expected_sha:
        return None, "env DETECTION_SHA does not match the routed value"
    if os.path.realpath(root) != os.path.realpath(
            vals["RELAY_ENGINE_RESULTS_ROOT"]):
        return None, "env RELAY_ENGINE_RESULTS_ROOT is not this root"
    return vals, ""

def publish_once(root_fd, name, data):
    # The entire owned-temp lifetime sits under one cleanup path: temp
    # names are collision-resistant (a pre-existing stale temp can never
    # block a retry), creation itself is inside the guarded region, and
    # a cleanup failure never replaces the primary outcome -- a primary
    # exception keeps propagating, a failure verdict keeps its message,
    # and only a clean publication downgrades on an uncleanable temp.
    fd, tmp, verdict, primary = -1, None, None, None
    try:
        for _ in range(32):
            cand = "%s.tmp.%s" % (name, os.urandom(8).hex())
            try:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
                tmp = cand
                break
            except FileExistsError:
                continue
        if tmp is None:
            verdict = (False, "temp creation kept colliding")
        else:
            write_all(fd, data)
            os.close(fd)
            fd = -1
            try:
                os.link(tmp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                os.fsync(root_fd)
                verdict = (True, "published")
            except FileExistsError:
                have, err = read_exact(root_fd, name, 65536)
                if have is None:
                    verdict = (False, err)
                elif have != data:
                    verdict = (False,
                               "destination exists with conflicting"
                               " content")
                else:
                    verdict = (True, "already-present")
    except BaseException as e:
        primary = e
    if fd != -1:
        try:
            os.close(fd)
        except OSError:
            pass
    if tmp is not None:
        try:
            os.unlink(tmp, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        except OSError as e:
            if primary is None and verdict is not None and verdict[0]:
                verdict = (False, "temp cleanup failed: %s" % e.strerror)
    if primary is not None:
        raise primary
    return verdict

def make_carrier(root, n, env_basename, expected_ref, expected_sha):
    if not valid_basename(env_basename):
        return 1, "env basename fails the basename rule"
    root_fd = open_root(root)
    try:
        vals, err = routed_env(root_fd, root, env_basename, expected_ref,
                               expected_sha)
        if vals is None:
            return 1, err
        env_abs = os.path.join(os.path.realpath(root), env_basename)
        payload = BOOT_TMPL.format(env=env_abs, c=vals["COORD_ROOT"],
                                   r=vals["RELAY_ENGINE_RESULTS_ROOT"],
                                   p=vals["PYTHON"],
                                   ref=vals["DETECTION_REF"],
                                   sha=vals["DETECTION_SHA"])
        name = carrier_name(n)
        ok, msg = publish_once(root_fd, name,
                               (payload + "\n").encode("utf-8"))
        if not ok:
            return 1, msg
        return 0, "carrier=%s payload-sha256=%s" % (name, body_hex(payload))
    finally:
        os.close(root_fd)

def read_carrier(root, n, expected_hex):
    root_fd = open_root(root)
    try:
        data, err = read_exact(root_fd, carrier_name(n), 4096)
    finally:
        os.close(root_fd)
    if data is None:
        return None, err
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data or any(b < 0x20 or b == 0x7f for b in data):
        return None, "carrier holds control bytes or is empty"
    if hashlib.sha256(data).hexdigest() != expected_hex:
        return None, "carrier bytes do not match the expected digest"
    return data.decode("utf-8"), ""

def bootstrap_if_absent(path, n, root, expected_hex):
    n = int(n)
    payload, err = read_carrier(root, n, expected_hex)
    if payload is None:
        return 1, err
    terminate(path)
    scanned, err = scan_current(path)
    if scanned is None:
        return 1, err
    boots = scanned[0]
    if n in boots:
        if boots[n] != expected_hex:
            return 1, "bootstrap %d exists with a different payload" % n
        return 0, "bootstrap-present"
    if n != max(boots, default=0) + 1:
        return 1, "non-successor bootstrap number %d" % n
    append_record(path, "bootstrap %d sha256=%s | %s"
                  % (n, expected_hex, payload))
    return 0, "bootstrap-appended"

def supersede_if_absent(path, n):
    n = int(n)
    terminate(path)
    scanned, err = scan_current(path)
    if scanned is None:
        return 1, err
    boots, sups = scanned[0], scanned[2]
    if n in sups:
        return 0, "supersession-present"
    if n not in boots:
        return 1, "supersession names absent bootstrap %d" % n
    append_record(path, "supersede bootstrap %d" % n)
    return 0, "supersession-appended"

def pass_if_absent(path, n, payload_hex, runner_hex):
    terminate(path)
    scanned, err = scan_current(path)
    if scanned is None:
        return 1, err
    if scanned[3].count((int(n), payload_hex, runner_hex)) == 0:
        append_record(path, "hostile-legs-pass %s payload-sha256=%s"
                      " runner-sha256=%s" % (n, payload_hex, runner_hex))
    return 0, "pass-present"

def binding_append(path, branch, bound, tip, prev, producer):
    if branch != TARGET_BRANCH:
        return 1, "binding branch must be exactly %s" % TARGET_BRANCH
    body = ("binding branch=%s bound_sha=%s branch_tip=%s"
            " previous_bound_sha=%s producer=%s"
            % (branch, bound, tip, prev, producer))
    if not BIND.fullmatch(body):
        return 1, "binding fields fail the grammar"
    terminate(path)
    scanned, err = scan_current(path)
    if scanned is None:
        return 1, err
    binds = scanned[4]
    if binds and binds[-1] == (bound, tip, prev, producer):
        return 0, "binding-present"
    if binds and bound == binds[-1][0]:
        # Already-current no-op: the latest binding already binds this sha,
        # so a rerun appends nothing and never manufactures successor
        # history -- the caller's next step is simply resolve.
        return 0, "binding-current"
    if binds:
        if producer != "rebind":
            return 1, "non-initial binding must have producer=rebind"
        if prev != binds[-1][0]:
            return 1, "previous_bound_sha does not link to the latest binding"
    else:
        if producer != "task0" or prev != "none":
            return 1, "initial binding must be producer=task0 previous=none"
    append_record(path, body)
    return 0, "binding-appended"

def latest_binding(path):
    scanned, err = scan_current(path)
    if scanned is None:
        return 1, err
    if not scanned[4]:
        return 1, "no binding block"
    b = scanned[4][-1]
    return 0, ("branch=%s\nbound_sha=%s\nbranch_tip=%s\n"
               "previous_bound_sha=%s\nproducer=%s"
               % ((TARGET_BRANCH,) + b))

def machinery_commit_only(worktree, detection_sha):
    rc, cnt = git_out(worktree, "rev-list", "--count",
                      "%s..HEAD" % detection_sha)
    if rc != 0:
        return False, "the detection sha is absent from worktree history"
    if cnt != "1":
        return False, ("pre-binding HEAD is %s commits past the detection"
                       " sha; only the machinery commit is permitted" % cnt)
    rc, names = git_out(worktree, "show", "--name-only", "--format=",
                        "HEAD")
    if rc != 0:
        return False, "HEAD commit is unreadable"
    if tuple(sorted(n for n in names.split("\n") if n)) \
            != tuple(sorted(MACHINERY_CENSUS)):
        return False, ("the single pre-binding commit does not match the"
                       " machinery census")
    return True, ""

def classify(path, worktree, detection_sha):
    # The six-arm worktree state table, decided entirely read-only: no Git
    # or proof mutation happens here, and the caller acts only on the
    # printed arm. Exit 0 = an actionable arm; exit 3 = a classified STOP
    # (fail-closed under && chaining); exit 2 = unclassifiable inputs.
    # A missing proof file is exactly a missing binding, so a true first
    # bind classifies without any installed program (R2 of the r31
    # review: the caller invokes the digest-pinned planner artifact).
    if not re.fullmatch(r"[0-9a-f]{40}", detection_sha):
        return 2, "detection sha must be forty lowercase hex"
    try:
        scanned, err = scan_current(path)
    except FileNotFoundError:
        scanned, err = (({}, {}, set(), [], [], [], [], []), "")
    if scanned is None:
        return 3, "arm=STOP-ANOMALOUS\nreason=proof scan refused: %s" % err
    boots, sups, binds, wits = (scanned[0], scanned[2], scanned[4],
                                scanned[7])
    rc, branch = git_out(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return 2, "worktree HEAD is unreadable"
    if branch != TARGET_BRANCH:
        return 3, ("arm=STOP-ANOMALOUS\nreason=worktree is not on %s"
                   % TARGET_BRANCH)
    rc, head = git_out(worktree, "rev-parse", "HEAD")
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        return 2, "worktree HEAD is unreadable"
    for state in ("rebase-merge", "rebase-apply"):
        rc, p = git_out(worktree, "rev-parse", "--git-path", state)
        if rc == 0 and os.path.exists(
                p if os.path.isabs(p) else os.path.join(worktree, p)):
            return 3, ("arm=STOP-ANOMALOUS\nreason=a rebase is in"
                       " progress; branch history is uncertain")
    if not binds:
        if head == detection_sha:
            return 0, ("arm=INITIAL-BIND\nnext_n=1\nreason=fresh worktree"
                       " at the detection sha")
        ok, why = machinery_commit_only(worktree, detection_sha)
        if ok:
            return 0, ("arm=INITIAL-BIND\nnext_n=1\nreason=interrupted"
                       " first bind: the machinery commit exists")
        return 3, "arm=STOP-PREBIND\nreason=%s" % why
    bound, rec_tip = binds[-1][0], binds[-1][1]
    current = [k for k in boots if k not in sups]
    next_n = max(boots, default=0) + 1
    if bound == detection_sha:
        ok, msg = git_truth(worktree, bound, rec_tip)
        if not ok:
            # A current binding proves the sequence ran through command
            # 13; Git-truth red on top of it is a reset or moved branch
            # and must stop, never resume (R3 of the r31 review).
            return 3, ("arm=STOP-ANOMALOUS\nreason=current binding with"
                       " broken git truth: %s" % msg)
        return 0, ("arm=CURRENT-BINDING\nn=%d\nreason=latest binding"
                   " already binds the detection sha"
                   % (max(current) if current else next_n))
    if git_out(worktree, "merge-base", "--is-ancestor", detection_sha,
               head)[0] == 0:
        want = None
        for w in wits:
            if w[0] == bound and w[1] == detection_sha:
                want = w
        if want is not None:
            wanted = () if want[3] == "none" \
                else tuple(sorted(want[3].split(",")))
            if patch_ids(worktree, "%s..HEAD" % detection_sha) == wanted:
                return 0, ("arm=REBASE-COMPLETE\nnext_n=%d\n"
                           "previous_bound_sha=%s\nreason=the witnessed"
                           " lineage is present on the new base"
                           % (next_n, bound))
        # New-base ancestry alone cannot tell a completed rebase from a
        # reset or recreated branch (R3 of the r31 review): without the
        # durable witness and its exact transplanted lineage, stop.
        return 3, ("arm=STOP-ANOMALOUS\nreason=new-base ancestry without"
                   " a matching rebase witness")
    if git_out(worktree, "merge-base", "--is-ancestor", rec_tip,
               head)[0] == 0:
        return 0, ("arm=PRE-REBASE\nnext_n=%d\nprevious_bound_sha=%s\n"
                   "reason=verified stale binding awaiting the rebase"
                   % (next_n, bound))
    return 3, ("arm=STOP-ANOMALOUS\nreason=the branch no longer carries"
               " the recorded binding lineage")

def witness_append(path, worktree, onto_sha):
    # The durable transition witness, appended by the PRE-REBASE arm
    # immediately before the plan's only rebase command: it records the
    # rebase-stable patch-id set of the old lineage so REBASE-COMPLETE
    # can later prove the transplanted history is the intended one.
    if not re.fullmatch(r"[0-9a-f]{40}", onto_sha):
        return 1, "onto sha must be forty lowercase hex"
    terminate(path)
    scanned, err = scan_current(path)
    if scanned is None:
        return 1, err
    binds, wits = scanned[4], scanned[7]
    if not binds:
        return 1, "no binding block to witness"
    bound, rec_tip = binds[-1][0], binds[-1][1]
    if bound == onto_sha:
        return 1, "onto sha equals the bound sha; no rebase to witness"
    ok, msg = git_truth(worktree, bound, rec_tip)
    if not ok:
        return 1, msg
    rc, head = git_out(worktree, "rev-parse", "HEAD")
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        return 1, "worktree HEAD is unreadable"
    ids = patch_ids(worktree, "%s..HEAD" % bound)
    if ids is None:
        return 1, "lineage patch-ids are unreadable"
    joined = ",".join(ids) if ids else "none"
    body = ("rebase-witness from=%s onto=%s old_tip=%s patch_ids=%s"
            % (bound, onto_sha, head, joined))
    if not WITNESS.fullmatch(body):
        return 1, "witness fields fail the grammar"
    if (bound, onto_sha, head, joined) in wits:
        return 0, "witness-present"
    append_record(path, body)
    return 0, "witness-appended"

def read_verified_runner(root_fd, runner_basename, runner_hex):
    data, err = read_exact(root_fd, runner_basename, 1 << 20)
    if data is None:
        return None, err
    if hashlib.sha256(data).hexdigest() != runner_hex:
        return None, "runner bytes do not match the pinned digest"
    return data, ""

def exec_runner(runner_bytes, argv, extra_env=None, pass_fds=()):
    # The verified bytes themselves execute, from memory via stdin: no
    # pathname is re-opened between the digest check and the run, so a
    # replacement after the check can never change what runs.
    e = dict(os.environ)
    if extra_env:
        e.update(extra_env)
    return subprocess.run([sys.executable, "-"] + list(argv),
                          input=runner_bytes, env=e, pass_fds=pass_fds,
                          capture_output=True)

def run_legs(proof, n, root, env_basename, runner_basename, runner_hex,
             expected_ref, expected_sha):
    if not (valid_basename(env_basename)
            and valid_basename(runner_basename)):
        return 1, "basename fails the basename rule"
    root_fd = open_root(root)
    try:
        vals, err = routed_env(root_fd, root, env_basename, expected_ref,
                               expected_sha)
        if vals is None:
            return 1, err
        runner_bytes, err = read_verified_runner(root_fd, runner_basename,
                                                 runner_hex)
        if runner_bytes is None:
            return 1, err
    finally:
        os.close(root_fd)
    rr = os.path.realpath(root)
    pfd = open_append(proof)
    try:
        # The runner writes its leg lines through this held descriptor
        # only -- no shell redirect ever opens the proof pathname.
        out = exec_runner(runner_bytes,
                          [str(int(n)), os.path.join(rr, env_basename),
                           "fd:%d" % pfd, vals["COORD_ROOT"],
                           vals["RELAY_ENGINE_RESULTS_ROOT"],
                           vals["PYTHON"], vals["DETECTION_REF"],
                           vals["DETECTION_SHA"]],
                          pass_fds=(pfd,))
    finally:
        os.close(pfd)
    sys.stderr.write(out.stderr.decode("utf-8", "replace"))
    if out.returncode != 0:
        return 1, "runner exited nonzero"
    return 0, "legs-ran"

def fault_leg(root, runner_basename, env_basename, runner_hex):
    if not (valid_basename(runner_basename)
            and valid_basename(env_basename)):
        return 1, "basename fails the basename rule"
    root_fd = open_root(root)
    try:
        runner_bytes, err = read_verified_runner(root_fd, runner_basename,
                                                 runner_hex)
        if runner_bytes is None:
            return 1, err
        # Scratch anonymization, scoped honestly (R1 of the r36 review;
        # the earlier absolute "no foreign entry is ever touched" and
        # "whole race class abolished" claims were false and are
        # withdrawn). The threat this defends is the UNTRUSTED runner:
        # the child is never told the scratch name (its argv carries only
        # `fd:<n>` and the root path) and it runs ONLY after the unlink
        # below and the `st_nlink == 0` gate, so it can neither find nor
        # reach the anonymized inode -- complete against the child. The
        # scratch is a collision-resistant random name (the same trust
        # model as publish_once's temps) created no-replace with O_EXCL:
        # a pre-existing entry at a guessed name is never overwritten (it
        # yields EEXIST and a fresh name is tried), and creation
        # exhaustion means nothing was created, so no proof-owned scratch
        # can poison a retry. The single unlink of the just-created name
        # is straight-line TRUSTED-PARENT code with no untrusted code
        # running in the create->unlink window. This platform has neither
        # funlinkat (no atomic unlink-if-fd-matches) nor O_TMPFILE (no
        # windowless anonymous seekable inode), so the window cannot be
        # closed mechanically; a same-UID actor able to interpose in it
        # (a separate process winning a 64-bit-name microsecond race, or
        # an in-process monkeypatch of the parent's own os.unlink) sits
        # inside the same-UID/same-process trust boundary this plan
        # already concedes (S13) and could, in that window, delete an
        # attacker's own plant and leave the inode under an escaped name.
        # The `st_nlink == 0` gate still guarantees no child ever
        # executes against a named inode, so no accepted escaped write is
        # possible in any case. This residual was escalated to and
        # accepted by the operator (recorded in the run's PLAN relays);
        # it was never self-accepted.
        sfd, scratch = -1, None
        for _ in range(64):
            cand = "tmp-runner-fault-proof.%s" % os.urandom(8).hex()
            try:
                sfd = os.open(cand, os.O_RDWR | os.O_CREAT | os.O_EXCL
                              | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
            except FileExistsError:
                continue
            except OSError as e:
                return 1, "scratch entry refused: %s" % e.strerror
            scratch = cand
            break
        if scratch is None:
            # Nothing was created: no proof-owned entry exists, so the
            # refusal is trivially retry-safe and touches no other entry.
            return 1, "scratch name space kept colliding"
        try:
            os.unlink(scratch, dir_fd=root_fd)
            if os.fstat(sfd).st_nlink != 0:
                return 1, ("scratch anonymity could not be established:"
                           " the held inode still carries a name")
            rr = os.path.realpath(root)
            # The child writes through the INHERITED descriptor (fd: mode,
            # the runner's only proof interface) against a now-anonymous
            # inode; there is no name to redirect, and the readback below
            # is from that same held object.
            out = exec_runner(runner_bytes,
                              ["1", os.path.join(rr, env_basename),
                               "fd:%d" % sfd, "x", rr, sys.executable,
                               "x", "x"],
                              {"V29_RUNNER_FAULT": "short-transcript"},
                              pass_fds=(sfd,))
            if out.returncode == 0:
                return 1, "fault run unexpectedly succeeded"
            if b"short write: 1 of " not in out.stderr:
                return (1, "fault run failed without the short-write"
                        " diagnostic")
            os.lseek(sfd, 0, os.SEEK_SET)
            data, _ = drop_torn(read_fd_bytes(sfd))
            scanned, err = scan(
                data.decode("utf-8", "replace").split("\n")[:-1])
            if scanned is None:
                return 1, err
            if any(scanned[i] for i in CLASS_INDEXES):
                return 1, "fault scratch unexpectedly holds records"
            return 0, "fault-leg-ok"
        finally:
            # In the normal (non-interposed) flow the held inode is
            # anonymous, so closing the descriptor is the whole cleanup.
            os.close(sfd)
    finally:
        os.close(root_fd)

def selftest(root):
    rh = "a" * 64
    sha_a, sha_b = "a" * 40, "b" * 40
    hx = body_hex
    def pl(tag):
        return "%s [%s]" % (tag, sha_a)
    def rl(body):
        return frame(body) + "\n"
    def rec(k, tag):
        return rl("bootstrap %d sha256=%s | %s" % (k, hx(pl(tag)), pl(tag)))
    def ps(k, tag):
        return rl("hostile-legs-pass %d payload-sha256=%s runner-sha256=%s"
                  % (k, hx(pl(tag)), rh))
    def sup(k):
        return rl("supersede bootstrap %d" % k)
    def bnd(bound, prev, producer, tip=None):
        return rl("binding branch=%s bound_sha=%s branch_tip=%s"
                  " previous_bound_sha=%s producer=%s"
                  % (TARGET_BRANCH, bound, tip or bound, prev, producer))
    def corrupt_field(line, idx):
        # line is one framed record ending in "\n"; field 1 = length,
        # field 2 = digest.
        parts = line.rstrip("\n").split(" ", 3)
        parts[idx] = (str(int(parts[idx]) + 1) if idx == 1
                      else ("0" * 64 if parts[idx][0] != "0" else "1" * 64))
        return " ".join(parts) + "\n"
    B0 = bnd(sha_a, "none", "task0")
    MISS1 = ("expected exactly one valid hostile-legs-pass for"
             " bootstrap 1, found %d")
    ZERO = ("zero unsuperseded records: interrupted rebind;"
            " rerun the record-placement command")
    CUR = "current=%d payload-sha256=%s"
    cases = [
        ("interior-corrupt-digest",
         corrupt_field(rec(1, "L1"), 2) + ps(1, "L1") + B0,
         1, "corrupt frame at line 1", False),
        ("interior-corrupt-length",
         corrupt_field(rec(1, "L1"), 1) + ps(1, "L1") + B0,
         1, "corrupt frame at line 1", False),
        ("valid", rec(1, "L1") + ps(1, "L1") + B0,
         0, CUR % (1, hx(pl("L1"))), False),
        ("valid-rebind", rec(1, "L1") + ps(1, "L1") + B0 + sup(1)
         + rec(2, "L2") + ps(2, "L2") + bnd(sha_a, sha_a, "rebind"),
         0, CUR % (2, hx(pl("L2"))), False),
        ("torn-fragment", rec(1, "L1") + ps(1, "L1") + B0 + sup(1)[:12],
         0, CUR % (1, hx(pl("L1"))), True),
        ("torn-complete-supersede", rec(1, "L1") + ps(1, "L1") + B0
         + sup(1).rstrip("\n"), 0, CUR % (1, hx(pl("L1"))), True),
        ("torn-complete-bootstrap", rec(1, "L1") + ps(1, "L1") + B0 + sup(1)
         + rec(2, "L2").rstrip("\n"), 1, ZERO, True),
        ("torn-complete-pass", rec(1, "L1") + B0
         + ps(1, "L1").rstrip("\n"), 1, MISS1 % 0, True),
        ("unframed-record-is-noise", "bootstrap 1 sha256=%s | %s\n"
         % (hx(pl("L1")), pl("L1")), 1, "no bootstrap record", False),
        ("missing-binding", rec(1, "L1") + ps(1, "L1"),
         1, "no binding block for the current bootstrap", False),
        ("wrong-bound-sha", rec(1, "L1") + ps(1, "L1")
         + bnd(sha_b, "none", "task0"),
         1, "latest binding does not bind the expected DETECTION_SHA",
         False),
        ("payload-without-sha",
         rl("bootstrap 1 sha256=%s | plain" % hx("plain"))
         + rl("hostile-legs-pass 1 payload-sha256=%s runner-sha256=%s"
              % (hx("plain"), rh)) + B0,
         1, "current payload does not carry the expected DETECTION_SHA",
         False),
        ("duplicate-number", rec(1, "L1") + rec(1, "L1") + ps(1, "L1") + B0,
         1, "duplicate bootstrap number 1", False),
        ("duplicate-supersession", rec(1, "L1") + sup(1) + sup(1)
         + rec(2, "L2") + ps(2, "L2") + B0,
         1, "duplicate supersession of bootstrap 1", False),
        ("gapped-numbers", rec(1, "L1") + rec(3, "L3") + ps(1, "L1") + B0,
         1, "bootstrap numbers are not exactly 1..k", False),
        ("absent-target", rec(1, "L1") + ps(1, "L1") + B0 + sup(9),
         1, "supersession names absent bootstrap 9", False),
        ("digest-mismatch", rl("bootstrap 1 sha256=%s | L1" % ("0" * 64)),
         1, "bootstrap 1 payload digest mismatch", False),
        ("missing-pass", rec(1, "L1") + B0, 1, MISS1 % 0, False),
        ("wrong-runner-pass", rec(1, "L1") + B0
         + rl(parse_line(ps(1, "L1").rstrip("\n")).replace(rh, "b" * 64)),
         1, MISS1 % 0, False),
        ("duplicate-pass", rec(1, "L1") + ps(1, "L1") + ps(1, "L1") + B0,
         1, MISS1 % 2, False),
        ("malformed-binding", rec(1, "L1") + ps(1, "L1")
         + rl("binding malformed-but-framed"),
         1, "malformed binding record", False),
        ("transcript-noise", "binding=1 leg=missing status=1\n"
         + rec(1, "L1") + "git output line\n" + ps(1, "L1") + B0,
         0, CUR % (1, hx(pl("L1"))), False),
        ("malformed-witness", rec(1, "L1") + ps(1, "L1") + B0
         + rl("rebase-witness malformed-but-framed"),
         1, "malformed rebase-witness record", False),
    ]
    d = tempfile.mkdtemp(prefix="resolve-selftest-", dir=root)
    bad = []
    if len(cases) != RESOLVE_TRIPLES:
        bad.append("cases table holds %d triples, RESOLVE_TRIPLES says %d"
                   % (len(cases), RESOLVE_TRIPLES))
    if not (os.path.realpath(d) + os.sep).startswith(
            os.path.realpath(root) + os.sep):
        shutil.rmtree(d)
        sys.stderr.write("self-test directory escaped the given root\n")
        return 1
    def expect(tag, got, want):
        if got != want:
            bad.append("%s: want %r, got %r" % (tag, want, got))
    def pfile(name, text, binary=False):
        p = os.path.join(d, name)
        with open(p, "wb" if binary else "w") as f:
            f.write(text)
        return p
    try:
        for name, content, wcode, wmsg, wtorn in cases:
            expect(name, resolve(pfile(name, content), rh, sha_a),
                   (wcode, wmsg, wtorn))
        vw = pfile("worktree-red", rec(1, "L1") + ps(1, "L1") + B0)
        expect("worktree-not-on-branch", resolve(vw, rh, sha_a, d),
               (1, "worktree is not on %s" % TARGET_BRANCH, False))
        for cls, body in (
                ("bootstrap", "bootstrap 1 sha256=%s | %s"
                 % (hx(pl("L1")), pl("L1"))),
                ("supersede", "supersede bootstrap 10"),
                ("pass", "hostile-legs-pass 1 payload-sha256=%s"
                 " runner-sha256=%s" % (hx(pl("L1")), rh)),
                ("invocation", "hostile-legs-invocation n=1"
                 " runner-sha256=%s" % rh),
                ("exit", "hostile-legs-exit n=1 status=0"),
                ("binding", "binding branch=%s bound_sha=%s branch_tip=%s"
                 " previous_bound_sha=none producer=task0"
                 % (TARGET_BRANCH, sha_a, sha_a)),
                ("witness", "rebase-witness from=%s onto=%s old_tip=%s"
                 " patch_ids=none" % (sha_a, sha_b, sha_a))):
            line = frame(body)
            for cut in range(1, len(line)):
                if parse_line(line[:cut]) is not None:
                    bad.append("truncation validated: %s cut=%d"
                               % (cls, cut))
                    break
        emb = frame("supersede bootstrap 1")
        outer = frame("binding-note holds %s inside" % emb)
        for cut in range(1, len(outer)):
            if parse_line(outer[:cut]) is not None:
                bad.append("embedded-frame truncation validated at %d" % cut)
        rs = pfile("recover-supersede", rec(1, "L1") + ps(1, "L1") + B0
                   + sup(1).rstrip("\n"))
        # A fully framed but non-newline-terminated record is not landed:
        # the terminator is part of the durable-write contract.
        expect("recover-supersede step", supersede_if_absent(rs, "1"),
               (0, "supersession-appended"))
        ten = "".join(rec(i, "L%d" % i) for i in range(1, 11)) \
            + "".join(sup(i) for i in range(1, 10)) + ps(10, "L10") + B0
        sd = pfile("short-supersede-digit",
                   ten + frame("supersede bootstrap 10")[:-7])
        expect("short-supersede-digit step", supersede_if_absent(sd, "10"),
               (0, "supersession-appended"))
        sp = pfile("short-pass", rec(1, "L1") + B0 + ps(1, "L1")[:-9])
        expect("short-pass retry", pass_if_absent(sp, "1", hx(pl("L1")), rh),
               (0, "pass-present"))
        expect("short-pass", resolve(sp, rh, sha_a),
               (0, CUR % (1, hx(pl("L1"))), False))
        real = pfile("identity-proof", rec(1, "L1") + ps(1, "L1") + B0)
        ln = os.path.join(d, "identity-proof-link")
        os.symlink(real, ln)
        try:
            resolve(ln, rh, sha_a)
            bad.append("symlinked proof read accepted")
        except OSError:
            pass
        try:
            append_record(ln, "noise")
            bad.append("symlinked proof write accepted")
        except OSError:
            pass
        fifo = os.path.join(d, "identity-proof-fifo")
        os.mkfifo(fifo)
        try:
            resolve(fifo, rh, sha_a)
            bad.append("fifo proof read accepted")
        except OSError as e:
            if "not a regular file" not in str(e):
                bad.append("fifo proof read: wrong refusal %r" % (e,))
        try:
            append_record(fifo, "noise")
            bad.append("fifo proof write accepted")
        except OSError:
            pass
        os.mkdir(os.path.join(d, "carrier-root"))
        cr = os.path.join(d, "carrier-root")
        with open(os.path.join(cr, "env.sh"), "w") as f:
            for k in ENV_KEYS:
                v = {"RELAY_ENGINE_RESULTS_ROOT": cr,
                     "DETECTION_REF": "refs/heads/x",
                     "DETECTION_SHA": sha_a,
                     "PYTHON": sys.executable}.get(k, "val-" + k)
                f.write("export %s='%s'\n" % (k, v))
        MC = lambda n: make_carrier(cr, n, "env.sh", "refs/heads/x", sha_a)
        expect("make-carrier", MC("1")[0], 0)
        expect("make-carrier-idempotent", MC("1")[0], 0)
        expect("stale-env-sha",
               make_carrier(cr, "9", "env.sh", "refs/heads/x", sha_b),
               (1, "env DETECTION_SHA does not match the routed value"))
        expect("stale-env-ref",
               make_carrier(cr, "9", "env.sh", "refs/heads/y", sha_a),
               (1, "env DETECTION_REF does not match the routed value"))
        expect("traversal-env-basename",
               make_carrier(cr, "9", "../outside-env.sh",
                            "refs/heads/x", sha_a),
               (1, "env basename fails the basename rule"))
        rfd = open_root(cr)
        try:
            vals, _ = read_env(rfd, "env.sh")
        finally:
            os.close(rfd)
        env_abs = os.path.join(os.path.realpath(cr), "env.sh")
        payload = BOOT_TMPL.format(env=env_abs, c=vals["COORD_ROOT"],
                                   r=vals["RELAY_ENGINE_RESULTS_ROOT"],
                                   p=vals["PYTHON"],
                                   ref=vals["DETECTION_REF"],
                                   sha=vals["DETECTION_SHA"])
        ih = hx(payload)
        got = read_carrier(cr, "1", ih)
        if got[0] != payload:
            bad.append("carrier readback mismatch")
        with open(os.path.join(cr, carrier_name(2)), "w") as f:
            f.write(payload[: len(payload) // 2] + "\n")
        expect("self-digested-shortened-carrier",
               read_carrier(cr, "2", ih),
               (None, "carrier bytes do not match the expected digest"))
        with open(os.path.join(cr, carrier_name(3)), "w") as f:
            f.write("A\rB\n")
        expect("cr-carrier", read_carrier(cr, "3", hx("A\rB")),
               (None, "carrier holds control bytes or is empty"))
        os.symlink(os.path.join(cr, carrier_name(1)),
                   os.path.join(cr, carrier_name(4)))
        if read_carrier(cr, "4", ih)[0] is not None:
            bad.append("symlinked carrier accepted")
        with open(os.path.join(cr, carrier_name(5)), "w") as f:
            f.write("conflicting\n")
        expect("conflicting-create-once", MC("5"),
               (1, "destination exists with conflicting content"))
        crfd = open_root(cr)
        try:
            pfile2 = os.path.join(cr, "pub-target")
            with open(pfile2, "w") as f:
                f.write("racer\n")
            expect("publish-once-conflict",
                   publish_once(crfd, "pub-target", b"mine\n"),
                   (False, "destination exists with conflicting content"))
            expect("publish-once-idempotent",
                   publish_once(crfd, "pub-target", b"racer\n"),
                   (True, "already-present"))
            real_link = os.link
            def eio_link(*a, **k):
                raise OSError(errno.EIO, "injected link failure")
            os.link = eio_link
            try:
                try:
                    publish_once(crfd, "retry-target", b"payload\n")
                    bad.append("injected link failure not raised")
                except OSError:
                    pass
            finally:
                os.link = real_link
            if any(x.startswith("retry-target.tmp.")
                   for x in os.listdir(cr)):
                bad.append("link-failure temp not cleaned")
            expect("publish-retry-after-link-failure",
                   publish_once(crfd, "retry-target", b"payload\n"),
                   (True, "published"))
            real_write = os.write
            os.write = lambda fd, buf: real_write(fd, buf[:1])
            try:
                try:
                    publish_once(crfd, "retry-target-2", b"payload\n")
                    bad.append("short publish write not detected")
                except OSError:
                    pass
            finally:
                os.write = real_write
            if any(x.startswith("retry-target-2.tmp.")
                   for x in os.listdir(cr)):
                bad.append("write-failure temp not cleaned")
            expect("publish-retry-after-write-failure",
                   publish_once(crfd, "retry-target-2", b"payload\n"),
                   (True, "published"))
            # A pre-existing temp-shaped entry can never block: names are
            # collision-resistant, and the foreign entry is not touched
            # (R4 of the r31 review's pre-existing-PID-temp probe).
            with open(os.path.join(cr, "retry-target-3.tmp.stale"),
                      "w") as f:
                f.write("foreign\n")
            expect("publish-ignores-stale-temp",
                   publish_once(crfd, "retry-target-3", b"payload\n"),
                   (True, "published"))
            with open(os.path.join(cr, "retry-target-3.tmp.stale")) as f:
                if f.read() != "foreign\n":
                    bad.append("stale foreign temp was modified")
            real_urandom = os.urandom
            os.urandom = lambda n: b"\xde\xad" * (n // 2)
            try:
                fixed = "retry-target-4.tmp." + (b"\xde\xad" * 4).hex()
                with open(os.path.join(cr, fixed), "w") as f:
                    f.write("occupied\n")
                expect("publish-collision-exhaustion",
                       publish_once(crfd, "retry-target-4", b"payload\n"),
                       (False, "temp creation kept colliding"))
                with open(os.path.join(cr, fixed)) as f:
                    if f.read() != "occupied\n":
                        bad.append("colliding entry was modified")
            finally:
                os.urandom = real_urandom
            expect("publish-after-collision-retry",
                   publish_once(crfd, "retry-target-4", b"payload\n"),
                   (True, "published"))
            # Cleanup failure never masks the primary outcome: with the
            # link failing EIO and the unlink failing EPERM, the EIO
            # propagates; with only the unlink failing, a successful
            # publication downgrades to a failed verdict and the retry
            # lands on already-present.
            real_link, real_unlink = os.link, os.unlink
            def eio_link2(*a, **k):
                raise OSError(errno.EIO, "injected link failure")
            def eperm_unlink(*a, **k):
                raise OSError(errno.EPERM, "injected unlink failure")
            os.link, os.unlink = eio_link2, eperm_unlink
            try:
                try:
                    publish_once(crfd, "retry-target-5", b"payload\n")
                    bad.append("primary link failure not raised")
                except OSError as e:
                    if e.errno != errno.EIO:
                        bad.append("cleanup failure replaced the primary"
                                   " error: %r" % (e,))
            finally:
                os.link, os.unlink = real_link, real_unlink
            expect("publish-after-cleanup-failure",
                   publish_once(crfd, "retry-target-5", b"payload\n"),
                   (True, "published"))
            os.unlink = eperm_unlink
            try:
                got = publish_once(crfd, "retry-target-6", b"payload\n")
                if got[0] is not False \
                        or not got[1].startswith("temp cleanup failed"):
                    bad.append("uncleanable temp not surfaced: %r"
                               % (got,))
            finally:
                os.unlink = real_unlink
            expect("publish-retry-lands-already-present",
                   publish_once(crfd, "retry-target-6", b"payload\n"),
                   (True, "already-present"))
        finally:
            os.close(crfd)
        lroot = os.path.join(d, "link-root")
        os.symlink(cr, lroot)
        try:
            open_root(lroot)
            bad.append("symlinked root accepted")
        except OSError:
            pass
        bp = pfile("bindings", "")
        expect("initial-binding",
               binding_append(bp, TARGET_BRANCH, sha_a, sha_a,
                              "none", "task0"), (0, "binding-appended"))
        expect("initial-binding-idempotent",
               binding_append(bp, TARGET_BRANCH, sha_a, sha_a,
                              "none", "task0"), (0, "binding-present"))
        expect("wrong-branch",
               binding_append(bp, "bad..branch", sha_b, sha_b,
                              sha_a, "rebind"),
               (1, "binding branch must be exactly %s" % TARGET_BRANCH))
        expect("other-branch",
               binding_append(bp, "other/branch", sha_b, sha_b,
                              sha_a, "rebind"),
               (1, "binding branch must be exactly %s" % TARGET_BRANCH))
        expect("rebind-binding",
               binding_append(bp, TARGET_BRANCH, sha_b, sha_b,
                              sha_a, "rebind"), (0, "binding-appended"))
        expect("rebind-idempotent",
               binding_append(bp, TARGET_BRANCH, sha_b, sha_b,
                              sha_a, "rebind"), (0, "binding-present"))
        expect("binding-current-no-op",
               binding_append(bp, TARGET_BRANCH, sha_b, "7" * 40,
                              sha_b, "rebind"), (0, "binding-current"))
        expect("non-initial-task0",
               binding_append(bp, TARGET_BRANCH, "4" * 40, "4" * 40,
                              sha_b, "task0"),
               (1, "non-initial binding must have producer=rebind"))
        expect("rebind-broken-linkage",
               binding_append(bp, TARGET_BRANCH, "3" * 40, "3" * 40,
                              "9" * 40, "rebind"),
               (1, "previous_bound_sha does not link to the latest binding"))
        expect("binding-grammar",
               binding_append(bp, TARGET_BRANCH, "short", "short",
                              "none", "task0"),
               (1, "binding fields fail the grammar"))
        expect("latest-binding", latest_binding(bp),
               (0, "branch=%s\nbound_sha=%s\nbranch_tip=%s\n"
                "previous_bound_sha=%s\nproducer=rebind"
                % (TARGET_BRANCH, sha_b, sha_b, sha_a)))
        fresh_bp = os.path.join(d, "bindings-fresh")
        expect("initial-rebind-refused",
               binding_append(fresh_bp, TARGET_BRANCH, sha_a, sha_a,
                              "none", "rebind"),
               (1, "initial binding must be producer=task0 previous=none"))
        expect("witness-bad-onto", witness_append(bp, d, "zz"),
               (1, "onto sha must be forty lowercase hex"))
        expect("witness-no-binding",
               witness_append(pfile("witness-none", ""), d, "9" * 40),
               (1, "no binding block to witness"))
        expect("witness-same-sha", witness_append(bp, d, sha_b),
               (1, "onto sha equals the bound sha; no rebase to witness"))
        expect("witness-non-repo", witness_append(bp, d, "9" * 40),
               (1, "worktree is not on %s" % TARGET_BRANCH))
        expect("classify-bad-sha", classify(bp, d, "nothex"),
               (2, "detection sha must be forty lowercase hex"))
        mb = pfile("classify-malformed", rl("binding malformed-but-framed"))
        expect("classify-scan-refusal", classify(mb, d, sha_a),
               (3, "arm=STOP-ANOMALOUS\nreason=proof scan refused:"
                " malformed binding record"))
        # The unreadable-HEAD usage path, exercised with a NONEXISTENT
        # worktree so `git -C` fails (rc != 0) regardless of whether the
        # results root sits inside a Git checkout. Passing the confined
        # temp dir here would instead be discovered as part of an
        # enclosing repository when the plan-mandated results root lives
        # under $COORD_ROOT, yielding the branch-shape STOP rather than
        # the usage result (the r36-review-era leg's environmental
        # assumption; corrected per the Task 0 command-6 blocker).
        expect("classify-non-repo",
               classify(os.path.join(d, "classify-absent"),
                        os.path.join(d, "no-such-worktree"), sha_a),
               (2, "worktree HEAD is unreadable"))
        stub_ok = pfile("stub-ok.py", "import sys\n"
                        "sys.stderr.write('short write: 1 of 9 bytes')\n"
                        "sys.exit(1)\n")
        stub_bad = pfile("stub-bad.py", "import sys\nsys.exit(3)\n")
        def fhx(p):
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        expect("fault-leg-stub",
               fault_leg(d, "stub-ok.py", "unused", fhx(stub_ok)),
               (0, "fault-leg-ok"))
        expect("fault-leg-unrelated",
               fault_leg(d, "stub-bad.py", "unused", fhx(stub_bad)),
               (1, "fault run failed without the short-write diagnostic"))
        expect("fault-leg-wrong-digest",
               fault_leg(d, "stub-ok.py", "unused", "c" * 64),
               (1, "runner bytes do not match the pinned digest"))
        FORGE = (
            "bootstrap 1 sha256=%s | %s" % (hx(pl("F")), pl("F")),
            "supersede bootstrap 1",
            "hostile-legs-pass 1 payload-sha256=%s runner-sha256=%s"
            % (hx(pl("F")), rh),
            "hostile-legs-invocation n=1 runner-sha256=%s" % rh,
            "hostile-legs-exit n=1 status=0",
            "binding branch=%s bound_sha=%s branch_tip=%s"
            " previous_bound_sha=none producer=task0"
            % (TARGET_BRANCH, sha_a, sha_a),
            "rebase-witness from=%s onto=%s old_tip=%s patch_ids=none"
            % (sha_a, sha_b, sha_a))
        for i, fbody in enumerate(FORGE):
            fline = (frame(fbody) + "\n").encode("utf-8")
            sf = pfile("stub-forge-%d.py" % i,
                       "import os, sys\n"
                       "os.write(int(sys.argv[3][3:]), %r)\n"
                       "sys.stderr.write('short write: 1 of 9 bytes')\n"
                       "sys.exit(1)\n" % (fline,))
            expect("fault-leg-forged-class-%d" % i,
                   fault_leg(d, "stub-forge-%d.py" % i, "unused", fhx(sf)),
                   (1, "fault scratch unexpectedly holds records"))
        # The fixed-name scratch lifecycle is abolished (R1 of the r35
        # review), so there is no predictable name to pre-plant, swap, or
        # rename out, and no name-based cleanup that could delete an
        # unowned entry. A pinned-randomness probe forces every candidate
        # name to collide with a pre-existing UNOWNED entry: O_EXCL
        # refuses each candidate, exhaustion refuses BEFORE any execution
        # (the marker stub proves no child ran), NOTHING is created so no
        # proof-owned scratch can poison a rerun and no foreign entry is
        # ever touched, the pre-existing entry survives byte-identical,
        # and once the collision condition lifts the very next invocation
        # reaches its normal accepted result. This subsumes the r32-r34
        # quarantine swap/exhaustion classes -- they cannot arise when
        # exhaustion means nothing was created.
        marker = pfile("stub-marker.py",
                       "import os, sys\n"
                       "open(os.path.join(sys.argv[5],"
                       " 'child-ran-marker'), 'w').close()\n"
                       "sys.stderr.write('short write: 1 of 9 bytes')\n"
                       "sys.exit(1)\n")
        real_urandom = os.urandom
        os.urandom = lambda n: b"\xaa" * n
        fixedname = "tmp-runner-fault-proof." + ("aa" * 8)
        with open(os.path.join(d, fixedname), "w") as f:
            f.write("UNOWNED-SCRATCH-EVIDENCE")
        try:
            expect("fault-leg-name-collision",
                   fault_leg(d, "stub-marker.py", "unused", fhx(marker)),
                   (1, "scratch name space kept colliding"))
        finally:
            os.urandom = real_urandom
        with open(os.path.join(d, fixedname)) as f:
            if f.read() != "UNOWNED-SCRATCH-EVIDENCE":
                bad.append("colliding scratch entry was not preserved"
                           " byte-identical")
        if os.path.exists(os.path.join(d, "child-ran-marker")):
            bad.append("name collision still executed the child")
        expect("fault-leg-retry-after-collision",
               fault_leg(d, "stub-marker.py", "unused", fhx(marker)),
               (0, "fault-leg-ok"))
        if not os.path.exists(os.path.join(d, "child-ran-marker")):
            bad.append("post-collision retry did not reach execution")
        os.unlink(os.path.join(d, "child-ran-marker"))
        os.unlink(os.path.join(d, fixedname))
        # A pre-existing symlink or FIFO at a guessed name is refused by
        # O_EXCL|O_NOFOLLOW -- never followed, never replaced -- and
        # exhausts to the same collision refusal with the entry intact.
        for kind, mk in (("symlink",
                          lambda p: os.symlink("outside-target", p)),
                         ("fifo", os.mkfifo)):
            os.urandom = lambda n: b"\xbb" * n
            gname = "tmp-runner-fault-proof." + ("bb" * 8)
            mk(os.path.join(d, gname))
            try:
                expect("fault-leg-collision-%s" % kind,
                       fault_leg(d, "stub-marker.py", "unused",
                                 fhx(marker)),
                       (1, "scratch name space kept colliding"))
            finally:
                os.urandom = real_urandom
            if not os.path.lexists(os.path.join(d, gname)):
                bad.append("collision %s entry was destroyed" % kind)
            os.unlink(os.path.join(d, gname))
        # A green run leaves no scratch residue in the root: the
        # anonymized inode had no name, and closing the descriptor is the
        # whole cleanup.
        expect("fault-leg-green-no-residue",
               fault_leg(d, "stub-marker.py", "unused", fhx(marker)),
               (0, "fault-leg-ok"))
        os.unlink(os.path.join(d, "child-ran-marker"))
        if any(x.startswith("tmp-runner-fault-proof")
               for x in os.listdir(d)):
            bad.append("green fault-leg left a scratch residue")
        # The r36-review post-create/pre-unlink interleaving, committed
        # exactly (R1 of the r36 review). This is a PARENT-process attack:
        # os.unlink is monkeypatched to fire in the create->unlink window,
        # rename the proof-owned inode to an escaped name, and plant a
        # foreign entry at the scratch name before the real unlink runs.
        # The GUARANTEED property is asserted: the `st_nlink == 0` gate
        # refuses, so NO child executes and NO accepted verdict emits.
        # The DOCUMENTED residual (routed to the operator, not asserted as
        # safe): this platform lacks funlinkat/O_TMPFILE, so the parent's
        # name-unlink in the window may delete the attacker's plant and
        # leave the inode escaped -- reachable only by compromising the
        # trusted parent, inside the same-UID trust boundary.
        real_unlink = os.unlink
        real_rename = os.rename
        swept = {}
        def swap_unlink(p, *a, **k):
            if isinstance(p, str) \
                    and p.startswith("tmp-runner-fault-proof.") \
                    and k.get("dir_fd") is not None and not swept:
                swept["hit"] = True
                real_rename(p, "escaped-proof-owned",
                            src_dir_fd=k["dir_fd"], dst_dir_fd=k["dir_fd"])
                ffd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                              0o644, dir_fd=k["dir_fd"])
                os.write(ffd, b"UNOWNED-SCRATCH-EVIDENCE")
                os.close(ffd)
            return real_unlink(p, *a, **k)
        os.unlink = swap_unlink
        try:
            got = fault_leg(d, "stub-marker.py", "unused", fhx(marker))
        finally:
            os.unlink = real_unlink
        if got != (1, "scratch anonymity could not be established:"
                   " the held inode still carries a name"):
            bad.append("parent-swap interleaving did not refuse: %r"
                       % (got,))
        if not swept.get("hit"):
            bad.append("parent-swap interleaving never fired")
        if os.path.exists(os.path.join(d, "child-ran-marker")):
            bad.append("parent-swap interleaving executed the child")
        # Test hygiene: clear whatever the documented residual left.
        for x in list(os.listdir(d)):
            if x.startswith("tmp-runner-fault-proof") \
                    or x in ("escaped-proof-owned", "child-ran-marker"):
                try:
                    os.unlink(os.path.join(d, x))
                except (FileNotFoundError, IsADirectoryError):
                    pass
        def cstub(name, text):
            p = os.path.join(cr, name)
            with open(p, "w") as f:
                f.write(text)
            return p
        def fhx2(p):
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        green = cstub("stub-legs.py",
                      "import os, sys\n"
                      "os.write(int(sys.argv[3][3:]),"
                      " b'binding=1 leg=missing status=1\\n')\n"
                      "sys.exit(0)\n")
        lp = os.path.join(d, "legs-proof")
        RL = lambda proof, name, hexv, sha: run_legs(
            proof, "1", cr, "env.sh", name, hexv, "refs/heads/x", sha)
        expect("run-legs-green", RL(lp, "stub-legs.py", fhx2(green), sha_a),
               (0, "legs-ran"))
        with open(lp) as f:
            if "binding=1 leg=missing status=1" not in f.read():
                bad.append("run-legs held-descriptor line absent")
        nz = cstub("stub-legs-nz.py", "import sys\nsys.exit(3)\n")
        expect("run-legs-nonzero", RL(lp, "stub-legs-nz.py", fhx2(nz),
                                      sha_a), (1, "runner exited nonzero"))
        expect("run-legs-wrong-digest",
               RL(lp, "stub-legs.py", "c" * 64, sha_a),
               (1, "runner bytes do not match the pinned digest"))
        expect("run-legs-stale-routed-sha",
               RL(lp, "stub-legs.py", fhx2(green), sha_b),
               (1, "env DETECTION_SHA does not match the routed value"))
        lpl = os.path.join(d, "legs-proof-link")
        os.symlink(lp, lpl)
        try:
            RL(lpl, "stub-legs.py", fhx2(green), sha_a)
            bad.append("run-legs symlinked proof accepted")
        except OSError:
            pass
        lpf = os.path.join(d, "legs-proof-fifo")
        os.mkfifo(lpf)
        try:
            RL(lpf, "stub-legs.py", fhx2(green), sha_a)
            bad.append("run-legs fifo proof accepted")
        except OSError as e:
            if "not a regular file" not in str(e):
                bad.append("run-legs fifo proof: wrong refusal %r" % (e,))
        imp_hex = fhx2(green)
        with open(green, "w") as f:
            f.write("import sys\nsys.exit(0)\n")
        expect("run-legs-replaced-after-pin",
               RL(lp, "stub-legs.py", imp_hex, sha_a),
               (1, "runner bytes do not match the pinned digest"))
        selfswap = cstub(
            "stub-selfswap.py",
            "import os, sys\n"
            "open(os.path.join(sys.argv[5], 'stub-selfswap.py'),"
            " 'w').write('swapped')\n"
            "os.write(int(sys.argv[3][3:]),"
            " b'binding=1 leg=selfswap status=1\\n')\n"
            "sys.exit(0)\n")
        ss_hex = fhx2(selfswap)
        expect("run-legs-swap-mid-run-still-verified-bytes",
               RL(lp, "stub-selfswap.py", ss_hex, sha_a), (0, "legs-ran"))
        with open(lp) as f:
            if "binding=1 leg=selfswap status=1" not in f.read():
                bad.append("mid-run swap leg line absent")
        expect("run-legs-reverify-per-invocation",
               RL(lp, "stub-selfswap.py", ss_hex, sha_a),
               (1, "runner bytes do not match the pinned digest"))
        fresh = os.path.join(d, "fresh")
        expect("fresh-bootstrap",
               bootstrap_if_absent(fresh, "1", cr, ih),
               (0, "bootstrap-appended"))
        expect("fresh-bootstrap-idempotent",
               bootstrap_if_absent(fresh, "1", cr, ih),
               (0, "bootstrap-present"))
        expect("carrier-mismatch-refuses",
               bootstrap_if_absent(fresh, "2", cr, hx("other"))[0], 1)
        pp = pfile("pass-prefix", rec(1, "L1") + B0
                   + frame("hostile-legs-pass 1 payl")[:30])
        pass_if_absent(pp, "1", hx(pl("L1")), rh)
        pass_if_absent(pp, "1", hx(pl("L1")), rh)
        expect("pass-prefix", resolve(pp, rh, sha_a),
               (0, CUR % (1, hx(pl("L1"))), False))
        sw = os.path.join(d, "short-write")
        real_write = os.write
        os.write = lambda fd, buf: real_write(fd, buf[:1])
        try:
            append_record(sw, "record")
            bad.append("short-write: not detected")
        except OSError:
            pass
        finally:
            os.write = real_write
    finally:
        shutil.rmtree(d)
    if os.path.exists(d):
        bad.append("self-test directory not removed")
    if bad:
        sys.stderr.write("\n".join(bad) + "\n")
        return 1
    return 0

SUBCOMMANDS = {"self-test": 1, "terminate": 1, "append": 2,
               "make-carrier": 5, "bootstrap-if-absent": 4,
               "supersede-if-absent": 2, "pass-if-absent": 4,
               "binding-append": 6, "latest-binding": 1,
               "witness-append": 3, "classify": 3,
               "fault-leg": 4, "run-legs": 8, "resolve": 4}

def main(argv):
    if len(argv) < 2 or argv[1] not in SUBCOMMANDS:
        sys.stderr.write("unknown subcommand\n")
        return 2
    cmd = argv[1]
    if len(argv) - 2 != SUBCOMMANDS[cmd]:
        sys.stderr.write("wrong argument count for %s\n" % cmd)
        return 2
    if cmd == "self-test":
        return selftest(argv[2])
    if cmd == "terminate":
        terminate(argv[2])
        return 0
    if cmd == "append":
        append_record(argv[2], argv[3])
        return 0
    if cmd == "make-carrier":
        code, msg = make_carrier(argv[2], argv[3], argv[4], argv[5],
                                 argv[6])
    elif cmd == "bootstrap-if-absent":
        code, msg = bootstrap_if_absent(argv[2], argv[3], argv[4], argv[5])
    elif cmd == "supersede-if-absent":
        code, msg = supersede_if_absent(argv[2], argv[3])
    elif cmd == "pass-if-absent":
        code, msg = pass_if_absent(argv[2], argv[3], argv[4], argv[5])
    elif cmd == "binding-append":
        code, msg = binding_append(argv[2], argv[3], argv[4], argv[5],
                                   argv[6], argv[7])
    elif cmd == "latest-binding":
        code, msg = latest_binding(argv[2])
    elif cmd == "witness-append":
        code, msg = witness_append(argv[2], argv[3], argv[4])
    elif cmd == "classify":
        code, msg = classify(argv[2], argv[3], argv[4])
    elif cmd == "fault-leg":
        code, msg = fault_leg(argv[2], argv[3], argv[4], argv[5])
    elif cmd == "run-legs":
        code, msg = run_legs(argv[2], argv[3], argv[4], argv[5], argv[6],
                             argv[7], argv[8], argv[9])
    elif cmd == "resolve":
        code, msg, torn = resolve(argv[2], argv[3], argv[4], argv[5])
        if torn:
            sys.stderr.write("torn-tail flagged: final bytes lack a"
                             " newline; recovery is terminate-then-"
                             "reassess via the -if-absent subcommands\n")
    (sys.stdout if code == 0 else sys.stderr).write(msg + "\n")
    return code

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except OSError as e:
        sys.stderr.write("io failure: %s\n" % e)
        sys.exit(1)
