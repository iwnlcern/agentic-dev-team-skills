"""Plan an operator check installation and supply the standalone installer text."""
import base64
from collections.abc import Callable
import json
import os
from pathlib import Path
import stat
from typing import Literal, NamedTuple

INTERPRETER_CANDIDATES = {
    'darwin': ('shim:/usr/bin/python3', '/Library/Developer/CommandLineTools/usr/bin/python3'),
    'linux': ('/usr/bin/python3',),
}
CHECK_DIR = '/opt/adt-operator'
CHECK_PATH = CHECK_DIR + '/operator-check'
KEY_DIR = CHECK_DIR + '/key'
KEY_PATH = KEY_DIR + '/operator-check'
PUBLIC_PATH = CHECK_DIR + '/operator-check.pub'
SUDOERS_PATH = '/etc/sudoers.d/adt-operator-check'
PAM_PATH = '/etc/pam.d/adt-operator-check'
SUDOERS = (b'Defaults!/opt/adt-operator/operator-check timestamp_timeout=0\n'
           b'Defaults!/opt/adt-operator/operator-check pam_service=adt-operator-check\n')
PAM = {
    'darwin': (b'auth required pam_opendirectory.so\naccount required pam_permit.so\n'
               b'password required pam_deny.so\nsession required pam_permit.so\n'),
    'linux': b'auth required pam_unix.so\naccount required pam_unix.so\n',
}
HELPER_SHEBANG = b'#!/usr/bin/python3 -IS\n'


class InstallRefused(Exception):
    def __init__(self, reason: str, failures: tuple[str, ...] = ()):
        self.reason = reason
        self.failures = failures
        super().__init__(reason if not failures else reason + ': ' + ', '.join(failures))


class PlannedFile(NamedTuple):
    path: str
    mode: int
    content: bytes | None
    kind: Literal['dir', 'file', 'keypair']


def chain_failure(path: str, *, lstat: Callable) -> str | None:
    target = Path(path)
    if (not target.is_absolute() or any(character.isspace() or character == '\0' for character in path)
            or '..' in target.parts):
        return path
    for member in (target, *target.parents):
        try:
            info = lstat(str(member))
        except OSError:
            return str(member)
        expected = stat.S_ISREG if member == target else stat.S_ISDIR
        if info.st_uid != 0 or info.st_mode & 0o022 or not expected(info.st_mode):
            return str(member)
    return None


def choose_interpreter(platform: str, *, read_executable: Callable[[str], str],
                       read_version: Callable[[str], tuple[int, int]], lstat: Callable) -> str:
    failures = []
    for candidate in INTERPRETER_CANDIDATES.get(platform, ()):
        try:
            path = read_executable(candidate[5:]) if candidate.startswith('shim:') else os.path.realpath(candidate)
            unsafe = chain_failure(path, lstat=lstat)
            if unsafe is not None:
                failures.append(f'{candidate}: {unsafe}')
            elif read_version(path) < (3, 9):
                failures.append(f'{candidate}: Python 3.9 or later is required')
            else:
                return path
        except (OSError, ValueError) as error:
            failures.append(f'{candidate}: {error}')
    if platform == 'darwin':
        failures.append("Install Apple's Command Line Tools (xcode-select --install), then run this again.")
    raise InstallRefused('interpreter_unsafe', tuple(failures))


def install_plan(platform: str, helper: bytes, interpreter: str) -> tuple[PlannedFile, ...]:
    if (platform not in PAM or not helper.startswith(HELPER_SHEBANG)
            or not os.path.isabs(interpreter) or any(character.isspace() for character in interpreter)):
        raise InstallRefused('operator_check_unsafe_path')
    return (
        PlannedFile(CHECK_DIR, 0o755, None, 'dir'),
        PlannedFile(CHECK_PATH, 0o755, f'#!{interpreter} -IS\n'.encode() + helper[len(HELPER_SHEBANG):], 'file'),
        PlannedFile(KEY_DIR, 0o700, None, 'dir'),
        PlannedFile(KEY_PATH, 0o600, None, 'keypair'),
        PlannedFile(PUBLIC_PATH, 0o644, None, 'keypair'),
        PlannedFile(SUDOERS_PATH, 0o440, SUDOERS, 'file'),
        PlannedFile(PAM_PATH, 0o644, PAM[platform], 'file'),
    )


def invocation_payload(platform: str, interpreter: str, operator: str,
                       files: tuple[PlannedFile, ...], *, root: str = '/', owner_uid: int = 0) -> bytes:
    members = [
        {'path': item.path, 'mode': item.mode, 'kind': item.kind,
         'content_b64': None if item.content is None else base64.b64encode(item.content).decode('ascii')}
        for item in files
    ]
    return json.dumps({'v': 1, 'root': root, 'owner_uid': owner_uid, 'operator': operator,
                       'platform': platform, 'interpreter': interpreter, 'files': members}).encode('utf-8')


# The launcher passes this program as the fixed -c argument. Standard input is only data.
INSTALLER_SOURCE = r'''
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

CHECK = '/opt/adt-operator'
HELPER = CHECK + '/operator-check'
KEYDIR = CHECK + '/key'
PRIVATE = KEYDIR + '/operator-check'
PUBLIC = CHECK + '/operator-check.pub'
SUDOERS = '/etc/sudoers.d/adt-operator-check'
PAM = '/etc/pam.d/adt-operator-check'
SUFFIX = '.adt-tmp'
SHAPE = {CHECK: (0o755, 'dir'), HELPER: (0o755, 'file'), KEYDIR: (0o700, 'dir'),
         PRIVATE: (0o600, 'keypair'), PUBLIC: (0o644, 'keypair'),
         SUDOERS: (0o440, 'file'), PAM: (0o644, 'file')}
DIRECTORIES = {CHECK, KEYDIR, KEYDIR + SUFFIX}
CHILDREN = {
    CHECK: {'operator-check', 'operator-check.adt-tmp', 'operator-check.pub', 'key', 'key.adt-tmp'},
    KEYDIR: {'operator-check'},
    KEYDIR + SUFFIX: {'operator-check', 'operator-check.pub'},
}
START = []
CHANGED = []


class Refused(Exception):
    def __init__(self, reason, paths=()):
        self.reason = reason
        self.paths = list(paths)


def decode():
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError()
    value = json.loads(raw.decode('utf-8'))
    types = {'v': int, 'root': str, 'owner_uid': int, 'operator': str,
             'platform': str, 'interpreter': str, 'files': list}
    if (type(value) is not dict or set(value) != set(types)
            or any(type(value[key]) is not kind for key, kind in types.items())
            or value['v'] != 1 or value['platform'] not in ('linux', 'darwin')):
        raise ValueError()
    root = Path(value['root'])
    uid = value['owner_uid']
    if (not root.is_absolute() or '..' in root.parts or uid != os.geteuid()
            or (uid == 0 and root != Path('/')) or (uid != 0 and root == Path('/'))):
        raise ValueError()
    if (not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*\$?', value['operator'])
            or not os.path.isabs(value['interpreter']) or any(c.isspace() for c in value['interpreter'])):
        raise ValueError()
    members = {}
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'path', 'mode', 'kind', 'content_b64'}
                or type(row['path']) is not str or type(row['mode']) is not int or type(row['kind']) is not str):
            raise ValueError()
        name, kind = row['path'], row['kind']
        if name in members or SHAPE.get(name) != (row['mode'], kind):
            raise ValueError()
        content = row['content_b64']
        if kind == 'file':
            if type(content) is not str:
                raise ValueError()
            content = base64.b64decode(content, validate=True)
        elif content is not None:
            raise ValueError()
        members[name] = content
    if set(members) != set(SHAPE) or not members[HELPER].startswith(
            ('#!' + value['interpreter'] + ' -IS\n').encode('utf-8')):
        raise ValueError()
    return root, uid, value['operator'], value['interpreter'], members


def checked(path, uid, directory, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise Refused('operator_check_unsafe_path', [str(path)])
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != uid or info.st_mode & 0o022 or not expected(info.st_mode):
        raise Refused('operator_check_unsafe_path', [str(path)])
    return True


def rooted(root, name):
    return root / name.lstrip('/')


def system_parent(root, name, uid):
    path = rooted(root, name).resolve(strict=True)
    stop = root.resolve(strict=True)
    if not path.is_relative_to(stop):
        raise Refused('operator_check_unsafe_path', [str(path)])
    while path != stop:
        checked(path, uid, True)
        path = path.parent
    checked(stop, uid, True)


def inventory(root, uid):
    for name in SHAPE:
        if checked(rooted(root, name), uid, name in DIRECTORIES, missing=True):
            START.append(name)
    for name, allowed in CHILDREN.items():
        path = rooted(root, name)
        if checked(path, uid, True, missing=True):
            if name != CHECK and stat.S_IMODE(path.lstat().st_mode) != 0o700:
                raise Refused('operator_check_unsafe_path', [str(path)])
            unknown = sorted(entry.name for entry in path.iterdir() if entry.name not in allowed)
            if unknown:
                raise Refused('operator_check_unsafe_path', [str(path / entry) for entry in unknown])
    for name in (HELPER, PAM, SUDOERS):
        checked(rooted(root, name + SUFFIX), uid, False, missing=True)
    for name in ('operator-check', 'operator-check.pub'):
        checked(rooted(root, KEYDIR + SUFFIX) / name, uid, False, missing=True)


def run(root, command):
    binary, *args = command
    return subprocess.run([str(rooted(root, binary)), *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                          timeout=30, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})


def permission(root, operator):
    result = run(root, ['/usr/bin/sudo', '-l', '-U', operator, HELPER])
    if result.returncode or HELPER.encode() not in result.stdout.split():
        raise Refused('not_permitted')


def write_staged(root, name, content, validate=None):
    path = rooted(root, name)
    temp = path.with_name(path.name + SUFFIX)
    mode, _ = SHAPE[name]
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if validate is not None:
        validate(temp)
    os.replace(temp, path)
    CHANGED.append(name)


def ensure_directory(root, name, mode):
    path = rooted(root, name)
    try:
        path.mkdir(mode)
    except FileExistsError:
        return False
    path.chmod(mode)
    CHANGED.append(name)
    return True


def install(root, uid, operator, interpreter, members):
    for name in ('/opt', '/etc/sudoers.d', '/etc/pam.d'):
        system_parent(root, name, uid)
    checked(rooted(root, '/usr/bin/ssh-keygen'), uid, False)
    if uid == 0:
        path = Path(interpreter)
        for member in (path, *path.parents):
            checked(member, uid, member != path)
    sudoers = rooted(root, '/etc/sudoers').read_text()
    if not re.search(r'^\s*(?:#|@)includedir\s+/etc/sudoers\.d/?(?:\s|$)', sudoers, re.MULTILINE):
        raise Refused('operator_check_unsafe_path', ['/etc/sudoers'])
    inventory(root, uid)
    if HELPER in START:
        permission(root, operator)
    else:
        made_dir = ensure_directory(root, CHECK, SHAPE[CHECK][0])
        write_staged(root, HELPER, members[HELPER])
        try:
            permission(root, operator)
        except Refused:
            rooted(root, HELPER).unlink()
            if made_dir:
                rooted(root, CHECK).rmdir()
            CHANGED.clear()
            raise
    if HELPER in START:
        write_staged(root, HELPER, members[HELPER])
    ensure_directory(root, KEYDIR, SHAPE[KEYDIR][0])
    stage = KEYDIR + SUFFIX
    ensure_directory(root, stage, 0o700)
    private = rooted(root, stage) / 'operator-check'
    public = private.with_suffix('.pub')
    private.unlink(missing_ok=True)
    public.unlink(missing_ok=True)
    result = run(root, ['/usr/bin/ssh-keygen', '-t', 'ed25519', '-N', '', '-C',
                        'adt-operator-check', '-f', str(private)])
    if result.returncode:
        raise Refused('operator_check_unsafe_path', [str(private)])
    for staged, name in ((private, PRIVATE), (public, PUBLIC)):
        checked(staged, uid, False)
        with staged.open('rb') as stream:
            os.fchmod(stream.fileno(), SHAPE[name][0])
            os.fsync(stream.fileno())
        os.replace(staged, rooted(root, name))
        CHANGED.append(name)
    rooted(root, stage).rmdir()
    write_staged(root, PAM, members[PAM])

    def validate(temp):
        result = run(root, ['/usr/sbin/visudo', '-c', '-f', str(temp)])
        if result.returncode:
            raise Refused('operator_check_unsafe_path', [str(temp)])

    write_staged(root, SUDOERS, members[SUDOERS], validate)
    return 'installed'


def uninstall(root, uid):
    for name in ('/opt', '/etc/sudoers.d', '/etc/pam.d'):
        system_parent(root, name, uid)
    inventory(root, uid)
    stage = KEYDIR + SUFFIX
    files = (SUDOERS, SUDOERS + SUFFIX, PAM, PAM + SUFFIX, HELPER, HELPER + SUFFIX,
             PRIVATE, PUBLIC, stage + '/operator-check', stage + '/operator-check.pub')
    for name, delete in [(name, Path.unlink) for name in files] + [
            (name, Path.rmdir) for name in (stage, KEYDIR, CHECK)]:
        try:
            delete(rooted(root, name))
        except FileNotFoundError:
            continue
        CHANGED.append(name)
    return 'removed'


def main():
    root, uid, operator, interpreter, members = decode()
    if sys.argv[1:] == ['decode']:
        rows = [{'path': name, 'mode': SHAPE[name][0], 'kind': SHAPE[name][1],
                 'sha256': None if content is None else hashlib.sha256(content).hexdigest()}
                for name, content in members.items()]
        return {'outcome': 'decoded', 'paths': rows, 'remaining': []}
    if sys.argv[1:] not in (['install'], ['uninstall']):
        raise ValueError()
    system_parent(root, '/opt', uid)
    parent = rooted(root, '/opt').resolve(strict=True)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused('operator_check_unsafe_path', ['operator check installation is busy']) from error
        if sys.argv[1] == 'install':
            outcome = install(root, uid, operator, interpreter, members)
        else:
            outcome = uninstall(root, uid)
    finally:
        os.close(descriptor)
    return {'outcome': outcome, 'paths': CHANGED, 'remaining': []}


try:
    result = main()
except Refused as failure:
    print(json.dumps({'outcome': failure.reason, 'paths': failure.paths, 'remaining': START}))
    sys.exit(1)
except (ValueError, OSError, subprocess.SubprocessError):
    print(json.dumps({'outcome': 'operator_check_unsafe_path', 'paths': [], 'remaining': START}))
    sys.exit(1)
print(json.dumps(result))
'''
UNINSTALLER_SOURCE = INSTALLER_SOURCE
