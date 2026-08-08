import os, shutil, subprocess, sys, tempfile
n, env_file, proof, c, r, p, ref, sha = sys.argv[1:9]
if not proof.startswith("fd:"):
    sys.stderr.write("proof must be an inherited descriptor, fd:<number>\n")
    sys.exit(2)
PFD = int(proof[3:])
PROG = (
    'ENGINE_ENV="$1" && [ -f "$ENGINE_ENV" ] && [ ! -L "$ENGINE_ENV" ] && . "$ENGINE_ENV" '
    '&& [ "$COORD_ROOT" = "$2" ] && [ "$RELAY_ENGINE_RESULTS_ROOT" = "$3" ] '
    '&& [ "$PYTHON" = "$4" ] && [ "$DETECTION_REF" = "$5" ] && [ "$DETECTION_SHA" = "$6" ]\n'
    'st=$?\n'
    'echo "status=$st"\n'
    'for v in COORD_ROOT RELAY_ENGINE_RESULTS_ROOT PYTHON DETECTION_REF DETECTION_SHA; do\n'
    '  eval "[ -z \\"\\${$v+x}\\" ]" && echo "$v=unset"\n'
    'done\n'
    'exit "$st"\n')
SENTINELS = ("COORD_ROOT", "RELAY_ENGINE_RESULTS_ROOT", "PYTHON",
             "DETECTION_REF", "DETECTION_SHA")
FAULT = os.environ.get("V29_RUNNER_FAULT") == "short-transcript"
def append_fsync(text):
    guard = b""
    size = os.fstat(PFD).st_size
    if size > 0:
        os.lseek(PFD, size - 1, os.SEEK_SET)
        if os.read(PFD, 1) != b"\n":
            guard = b"\n"
    os.lseek(PFD, 0, os.SEEK_END)
    buf = guard + text.encode("utf-8")
    done = os.write(PFD, buf[:1] if FAULT else buf)
    if done != len(buf):
        raise OSError("short write: %d of %d bytes" % (done, len(buf)))
    os.fsync(PFD)
failures = []
def leg(name, hostile):
    out = subprocess.run(["/bin/sh", "-c", PROG, "sh", hostile, c, r, p, ref, sha],
                         env={}, capture_output=True, text=True)
    lines = ["binding=%s leg=%s %s" % (n, name, l)
             for l in (out.stdout + out.stderr).splitlines()]
    append_fsync("\n".join(lines) + "\n")
    if out.returncode == 0:
        failures.append(name + ": hostile source chain unexpectedly succeeded")
    if name in ("missing", "symlink"):
        for v in SENTINELS:
            if "binding=%s leg=%s %s=unset" % (n, name, v) not in lines:
                failures.append(name + ": required sentinel line absent: " + v)
d = tempfile.mkdtemp(prefix="tmp-hostile-", dir=r)
try:
    leg("missing", os.path.join(d, "absent.sh"))
    link = os.path.join(d, "link.sh")
    os.symlink(env_file, link)
    leg("symlink", link)
    stale = os.path.join(d, "stale.sh")
    with open(env_file) as f, open(stale, "w") as g:
        for line in f:
            if line.startswith("export DETECTION_SHA="):
                g.write("export DETECTION_SHA='" + "0" * 40 + "'\n")
            else:
                g.write(line)
    leg("stale", stale)
finally:
    shutil.rmtree(d)
if failures:
    sys.stderr.write("\n".join(failures) + "\n")
    sys.exit(1)
sys.exit(0)
