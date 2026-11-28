"""Verify the installed terminal signer's SSH signature before minting a principal."""
import base64
import binascii
from collections.abc import Callable, Mapping
from hashlib import sha256, sha512
import os
from pathlib import Path
import re
import secrets
import stat as mode
import struct
from typing import NamedTuple

from relay_engine import ed25519_verify, jcs
from relay_engine.api_principals import OperatorPrincipal

_NAMESPACE = b'adt-terminal-check-v1'
_ALGORITHM = b'ssh-ed25519'
_KEY_PATH = '/opt/adt-operator/operator-check.pub'
_MAX_FRAME = 4096

PURPOSES = (
    'engine_stop', 'engine_start', 'project_stop', 'project_start',
    'restore', 'adopt_restored', 'abandonment',
    'new_folder_pass', 'answer_5d', 'report_confirmation',
    'authority.set_reapproval', 'authority.release_restore',
    'authority.assign_reviewer', 'authority.unbind_branch',
    'queue.answer', 'queue.open', 'queue.dismiss', 'queue.dismiss_all',
    'checkout_register', 'checkout_unregister', 'queue.go_ahead_without_review',
    'queue.waive_verification', 'slip.revoke', 'slip.reissue', 'seat_unseat',
)


class OperatorCheckRefused(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason if detail is None else f'{reason}: {detail}')
        self.reason = reason


class ActNotSent(Exception):
    def __init__(self, line: str | None):
        super().__init__(line)
        self.line = line


class ActAnswerLost(Exception):
    def __init__(self, line: str):
        super().__init__(line)
        self.line = line


class SshSig(NamedTuple):
    public_key: bytes
    namespace: str
    hash_alg: str
    signature: bytes


def build_message(purpose: str, args: Mapping, *, store_id: str, value: bytes) -> bytes:
    if (not isinstance(value, bytes) or len(value) != 32 or not re.fullmatch('[!-~]+', purpose)
            or not re.fullmatch('[ -~]+', store_id)):
        raise ValueError('invalid terminal confirmation fields')
    return b'\0'.join((_NAMESPACE, purpose.encode('ascii'), sha256(jcs.jcs_encode(dict(args))).hexdigest().encode(),
                       store_id.encode('ascii'), value.hex().encode()))


def _ssh_string(data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + data


def _strings(data: bytes, count: int) -> tuple[bytes, ...]:
    fields = []
    remaining = memoryview(data)
    for _ in range(count):
        if len(remaining) < 4:
            raise ValueError('truncated SSH field')
        size = struct.unpack_from('>I', remaining)[0]
        if len(remaining) - 4 < size:
            raise ValueError('truncated SSH field')
        fields.append(bytes(remaining[4:4 + size]))
        remaining = remaining[4 + size:]
    if remaining:
        raise ValueError('trailing SSH fields')
    return tuple(fields)


def _ed25519_key(blob: bytes) -> bytes:
    algorithm, key = _strings(blob, 2)
    if algorithm != _ALGORITHM or len(key) != 32:
        raise ValueError('invalid signer key')
    return key


def parse_sshsig(armored: bytes) -> SshSig:
    try:
        if not isinstance(armored, bytes) or len(armored) > _MAX_FRAME:
            raise ValueError('invalid SSH signature')
        lines = armored.splitlines()
        if (len(lines) < 3 or lines[0] != b'-----BEGIN SSH SIGNATURE-----'
                or lines[-1] != b'-----END SSH SIGNATURE-----' or not all(lines[1:-1])):
            raise ValueError('invalid SSH armor')
        blob = base64.b64decode(b''.join(lines[1:-1]), validate=True)
        if not blob.startswith(b'SSHSIG\x00\x00\x00\x01'):
            raise ValueError('invalid SSH signature version')
        key, namespace, reserved, hash_alg, signature = _strings(blob[10:], 5)
        algorithm, raw = _strings(signature, 2)
        if namespace != _NAMESPACE or reserved or hash_alg != b'sha512' or algorithm != _ALGORITHM or len(raw) != 64:
            raise ValueError('invalid SSH signature fields')
        return SshSig(_ed25519_key(key), namespace.decode('ascii'), hash_alg.decode('ascii'), raw)
    except (ValueError, binascii.Error) as error:
        raise OperatorCheckRefused('bad_signature') from error


def _installed_key(key_path: str, stat: Callable) -> bytes:
    path = Path(key_path)
    try:
        if not path.is_absolute():
            raise ValueError('key path must be absolute')
        for member in (path, *path.parents):
            info = stat(str(member))
            expected = mode.S_ISREG if member == path else mode.S_ISDIR
            if info.st_uid != 0 or info.st_mode & 0o022 or not expected(info.st_mode):
                raise ValueError('unsafe key path')
        with path.open('rb') as source:
            data = source.read(_MAX_FRAME + 1)
        if len(data) > _MAX_FRAME or len(data.splitlines()) != 1:
            raise ValueError('invalid key size')
        algorithm, blob, *_ = data.split()
        if algorithm != _ALGORITHM:
            raise ValueError('invalid key algorithm')
        return _ed25519_key(base64.b64decode(blob, validate=True))
    except (OSError, ValueError, binascii.Error) as error:
        raise OperatorCheckRefused('key_unsafe') from error


def _verify_signature(message: bytes, signature: bytes, *, key_path: str = _KEY_PATH,
                      stat: Callable = os.lstat) -> None:
    key = _installed_key(key_path, stat)
    parsed = parse_sshsig(signature)
    signed = b'SSHSIG' + b''.join(_ssh_string(value) for value in
                                  (_NAMESPACE, b'', b'sha512', sha512(message).digest()))
    if parsed.public_key != key or not ed25519_verify.verify(key, signed, parsed.signature):
        raise OperatorCheckRefused('bad_signature')


def _principal_after_verify(message: bytes, signature: bytes, *, key_path: str = _KEY_PATH,
                            stat: Callable = os.lstat) -> OperatorPrincipal:
    _verify_signature(message, signature, key_path=key_path, stat=stat)
    return OperatorPrincipal('terminal')


def require_operator(purpose: str, args: Mapping, *, store_id: str,
                     prove: Callable[[bytes], bytes] | None, show: Callable[[str], None],
                     act_line: str) -> OperatorPrincipal:
    if prove is None:
        raise OperatorCheckRefused('no_prove')
    show(act_line)
    message = build_message(purpose, args, store_id=store_id, value=secrets.token_bytes(32))
    return _principal_after_verify(message, prove(message))
