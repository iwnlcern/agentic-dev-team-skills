"""Installed approval-seal service; all item operations use the project flock."""
from contextlib import contextmanager
from dataclasses import replace
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import secrets
import sys
import threading

from . import seal_slots as slots, strings
from .seal_files import SealFolder, mac as _key_mac
from .jcs import jcs_encode
from .seal_types import (POINTER_UNREADABLE, PointerUnreadable, PointerMoved,
                         LedgerRebindRefused, SealUnusable, SealReport,
                         SealIdentityOneSided, require_canonical_id,
                         RecordInvalid, RecordUnreadable, SealedRecord,
                         ReservationEntry, UnreadableEntry, ReserveRefused)

_service = None
_install_lock = threading.Lock()
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_RECORD_CONTEXT = b'adt-approval-record-v1\0'
_ENTRY_CONTEXT = b'adt-approval-entry-v1\0'
_RECORD_FIELDS = {'format', 'project_id', 'chain_id', 'number', 'event', 'prev_mac', 'key_id'}
_ENTRY_FIELDS = {'format', 'project_id', 'chain_id', 'number', 'kind', 'subject',
                 'payload', 'runtime_instance_id', 'transaction', 'position', 'declared_kinds'}


def _no_float(value):
    raise ValueError('float_not_allowed')


def _object(data):
    if type(data) is not bytes:
        raise ValueError('object_bytes_required')
    value = json.loads(data.decode('utf-8'), parse_float=_no_float, parse_constant=_no_float)
    if type(value) is not dict or jcs_encode(value) != data:
        raise ValueError('canonical_object_required')
    return value


def _number(value):
    if type(value) is not int or value < 0:
        raise ValueError('nonnegative_integer_required')
    return value


def _hex(value, length):
    return type(value) is str and len(value) == length and all(c in '0123456789abcdef' for c in value)


def _operand_token(source, root) -> str | None:
    """Render the first operand without guessing any relative path's dir-fd."""
    if source.filename is None:
        return None
    name = os.fsdecode(source.filename)
    if os.path.isabs(name):
        absolute, anchor = os.path.abspath(name), os.path.abspath(root)
        if os.path.commonpath((absolute, anchor)) == anchor:
            name = os.path.relpath(absolute, anchor)
        else:
            name = os.path.basename(name)
    pieces = []
    for char in name:
        code = ord(char)
        if char == '\\':
            pieces.append('\\\\')
        elif 0x21 <= code <= 0x7e:
            pieces.append(char)
        elif code <= 0x7f:
            pieces.append(f'\\x{code:02x}')
        elif code <= 0xffff:
            pieces.append(f'\\u{code:04x}')
        else:
            pieces.append(f'\\U{code:08x}')
    token = ''.join(pieces)
    return token if len(token) <= 256 else token[:255] + '~'


def _raising_operation(source) -> str | None:
    """Return the deepest project frame of the original filesystem error."""
    operation = None
    frame = source.__traceback__
    while frame is not None:
        module = frame.tb_frame.f_globals.get('__name__', '')
        name = frame.tb_frame.f_code.co_name
        if (module.startswith('relay_engine.') and not module.startswith('relay_engine.tests.')
                and name.isidentifier()):
            operation = f"{module.removeprefix('relay_engine.')}.{name}"
        frame = frame.tb_next
    return operation


def production_root() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ('Library/Application Support/adt-approval-seal' if sys.platform == 'darwin'
                   else '.local/share/adt-approval-seal')


@contextmanager
def install(service):
    global _service
    with _install_lock:
        if _service is not None:
            raise RuntimeError('seal_already_installed')
        _service = service
    try:
        yield service
    finally:
        with _install_lock:
            _service = None


def _installed():
    if _service is None:
        raise RuntimeError('seal_not_installed')
    return _service


class SealService:
    def __init__(self, root, *, runtime_instance_id, clock_ms, report):
        require_canonical_id(runtime_instance_id)
        self.folder = SealFolder(root)
        self.runtime_instance_id = runtime_instance_id
        self.clock_ms, self.report = clock_ms, report
        self.renderer = None
        self._renderer_attached = False
        self._identity_warned = set()
        self.declarations = {}
        self._mismatch_projects = frozenset()

    def publish_mismatch_projects(self, projects: frozenset[str]) -> None:
        self._mismatch_projects = projects

    def attach_renderer(self, renderer):
        if self._renderer_attached:
            raise RuntimeError('seal_renderer_already_attached')
        self.renderer, self._renderer_attached = renderer, True

    def _report(self, project, chain, cause, errno_name=None):
        self.report(SealReport(project, chain, cause, self.clock_ms(), errno_name))

    def _fail(self, project, cause, chain=None, *, report=True, error=None):
        source = error
        if isinstance(error, slots.CannotRecord):
            source = error.__cause__
        errno_name = filename = operation = None
        if isinstance(source, OSError) and isinstance(source.errno, int):
            errno_name = errno.errorcode.get(source.errno, str(source.errno))
            try:
                filename, operation = _operand_token(source, self.folder.root), _raising_operation(source)
            except Exception:
                # Optional diagnostics must not replace the original refusal.
                filename = operation = None
        if report:
            self._report(project, chain, cause, errno_name)
        raise SealUnusable(cause, chain, errno_name, filename, operation) from None

    @contextmanager
    def _locked(self, project, *, exclusive, create=False, report=True):
        path = self.folder._project(project)
        try:
            if not create and not path.is_dir():
                raise OSError('missing_project')
            # The folder must support recording, even for a read-only call.
            if path.exists() and not self.folder.project_usable(project):
                raise OSError('unusable_project')
            with self.folder.lock(project, exclusive=exclusive):
                yield
        except OSError as error:
            self._fail(project, 'folder_unusable', report=report, error=error)

    @contextmanager
    def _open(self, project, name, *, write=False):
        fd = os.open(self.folder._project(project) / name,
                     _FLAGS | (os.O_RDWR if write else os.O_RDONLY))
        try:
            yield fd
        finally:
            os.close(fd)

    def _pointer(self, project):
        try:
            with self._open(project, 'pointer') as fd:
                state = slots.read_slots(fd, kind=slots.ItemKind.POINTER,
                                         binding=(project,), ledger_identity=None)
        except FileNotFoundError:
            return None
        if state.chain is None:
            return None
        try:
            return require_canonical_id(state.chain.decode('ascii'))
        except (ValueError, UnicodeError):
            raise slots.CannotRecord(7) from None

    def _pointer_or_damage(self, project):
        try:
            return self._pointer(project)
        except slots.CannotRecord:
            return POINTER_UNREADABLE

    def _compare(self, project, expected):
        found = self._pointer_or_damage(project)
        if found is POINTER_UNREADABLE and expected is not POINTER_UNREADABLE:
            self._fail(project, 'pointer_damaged')
        if found != expected:
            raise PointerMoved(expected, found)
        return found

    def _item(self, project, chain, kind, *, write=False):
        name = chain + ('.counter' if kind == slots.ItemKind.COUNTER else '.high-water')
        cause = 'counter_unusable' if kind == slots.ItemKind.COUNTER else 'high_water_unusable'
        try:
            with self._open(project, name, write=write) as fd:
                return slots.read_slots(fd, kind=kind, binding=(project, chain), ledger_identity=None)
        except (OSError, slots.CannotRecord) as error:
            found = self._pointer_or_damage(project)
            if found is None:
                self._report(project, None, 'pointer_unset')
            elif found is POINTER_UNREADABLE:
                self._report(project, None, 'pointer_damaged')
            self._fail(project, cause, chain, report=found == chain, error=error)

    def _cleanup(self, directory, name):
        # Called only inside this project's exclusive flock. Final names stay.
        for entry in os.listdir(directory):
            if entry.startswith('.creating-' + name + '.'):
                os.unlink(entry, dir_fd=directory)

    def _create_item(self, project, chain, kind, ledger_fd):
        suffix = '.counter' if kind == slots.ItemKind.COUNTER else '.high-water'
        name = chain + suffix
        directory = self.folder.project_dir(project)
        try:
            try:
                with self._open(project, name):
                    return False
            except FileNotFoundError:
                self._cleanup(directory, name)
                try:
                    fd = slots.create(directory, name, kind=kind, binding=(project, chain), value=0,
                                      identity_from=ledger_fd if kind == slots.ItemKind.COUNTER else None)
                except slots.CannotRecord:
                    self._fail(project, 'counter_unusable' if kind == slots.ItemKind.COUNTER
                               else 'high_water_unusable', chain)
                os.close(fd)
                return True
        finally:
            os.close(directory)

    def _abandoned(self, project, chain, kind, ledger_fd):
        name = chain + ('.counter' if kind == slots.ItemKind.COUNTER else '.high-water')
        cause = 'counter_unusable' if kind == slots.ItemKind.COUNTER else 'high_water_unusable'
        try:
            with self._open(project, name) as fd:
                state = slots.read_slots(fd, kind=kind, binding=(project, chain),
                                         ledger_identity=slots.file_identity(ledger_fd)
                                         if kind == slots.ItemKind.COUNTER else None)
                if (state.sequence != 0 or state.value != 0 or
                        os.pread(fd, 256, 0) != os.pread(fd, 256, 65536)):
                    raise slots.CannotRecord(7)
        except (OSError, slots.CannotRecord) as error:
            self._fail(project, cause, chain, error=error)

    def _cleanup_item(self, project, name):
        directory = self.folder.project_dir(project)
        try:
            self._cleanup(directory, name)
        finally:
            os.close(directory)

    def _new_pointer(self, project, chain):
        directory = self.folder.project_dir(project)
        fd = None
        try:
            self._cleanup(directory, 'pointer')
            temporary = '.creating-pointer.' + secrets.token_hex(8)
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | _FLAGS,
                         0o600, dir_fd=directory)
            data = slots.pair_bytes(slots.ItemKind.POINTER, (project,),
                                    slots.file_identity(fd), 0,
                                    chain.encode('ascii') if chain is not None else None)
            os.ftruncate(fd, slots.SIZE)
            slots._write(fd, data[:256], 0)
            slots._write(fd, data[65536:], 65536)
            slots._sync(fd, changing_length=True)
            os.link(temporary, 'pointer', src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            if sys.platform == 'darwin':
                slots._sync(fd, changing_length=True)
        except (slots.CannotRecord, OSError) as error:
            self._fail(project, 'folder_unusable', error=error)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)

    def _write_pointer(self, project, chain, *, damaged, create=False):
        try:
            with self._open(project, 'pointer', write=True) as fd:
                if damaged:
                    data = slots.pair_bytes(slots.ItemKind.POINTER, (project,),
                                            slots.file_identity(fd), 0,
                                            chain.encode('ascii') if chain is not None else None)
                    os.ftruncate(fd, slots.SIZE + 1)
                    slots._sync(fd, changing_length=True)
                    slots._write(fd, data[:256], 0)
                    slots._write(fd, data[65536:], 65536)
                    slots._sync(fd, changing_length=True)
                    os.ftruncate(fd, slots.SIZE)
                    slots._sync(fd, changing_length=True)
                else:
                    try:
                        state = slots.read_slots(fd, kind=slots.ItemKind.POINTER,
                                                 binding=(project,), ledger_identity=None)
                    except slots.CannotRecord:
                        self._fail(project, 'pointer_damaged')
                    slots.write_next(fd, state, value=0,
                                     chain=chain.encode('ascii') if chain is not None else None)
        except FileNotFoundError:
            if not create:
                self._fail(project, 'pointer_unset')
            self._new_pointer(project, chain)
        except (slots.CannotRecord, OSError) as error:
            self._fail(project, 'folder_unusable', error=error)

    def set_current(self, project, chain, *, ledger_fd, expected, bind):
        require_canonical_id(project); require_canonical_id(chain)
        _expected(expected, sentinel=True)
        if bind not in ('first', 'rebind'):
            raise ValueError('invalid_bind')
        os.fstat(ledger_fd)
        with self._locked(project, exclusive=True, create=expected is None):
            found = self._compare(project, expected)
            if found == chain:
                self._item(project, chain, slots.ItemKind.COUNTER)
                created = False
            else:
                created = self._create_item(project, chain, slots.ItemKind.COUNTER, ledger_fd)
            high_created = self._create_item(project, chain, slots.ItemKind.HIGH_WATER, ledger_fd)
            if not self.folder.usable_keys():
                self.folder.create_key()
            if not created:
                state = self._item(project, chain, slots.ItemKind.COUNTER)
                identity = slots.file_identity(ledger_fd)
                if found != chain:
                    self._abandoned(project, chain, slots.ItemKind.COUNTER, ledger_fd)
                elif bind == 'first':
                    if not slots.identities_match(state.ledger_identity, identity):
                        raise LedgerRebindRefused()
                else:
                    with self._open(project, chain + '.counter', write=True) as fd:
                        try:
                            slots.rewrite_identity(fd, state, identity)
                        except slots.CannotRecord as error:
                            self._fail(project, 'counter_unusable', chain, error=error)
            if not high_created and found != chain:
                self._abandoned(project, chain, slots.ItemKind.HIGH_WATER, ledger_fd)
            self._cleanup_item(project, chain + '.counter')
            self._cleanup_item(project, chain + '.high-water')
            self._cleanup_item(project, 'pointer')
            self._write_pointer(project, chain, damaged=found is POINTER_UNREADABLE, create=True)
            self._report(project, chain, None)

    def return_current(self, project, chain, *, expected):
        if expected is POINTER_UNREADABLE:
            raise TypeError('sentinel_not_a_preimage')
        require_canonical_id(project)
        if chain is not None:
            require_canonical_id(chain)
        _expected(expected, sentinel=False)
        with self._locked(project, exclusive=True):
            self._compare(project, expected)
            self._write_pointer(project, chain, damaged=False)
            self._report(project, chain, None)

    def clear_current(self, project, *, expected):
        require_canonical_id(project)
        _expected(expected, sentinel=True)
        if expected is None:
            raise TypeError('clear_requires_expected_chain')
        with self._locked(project, exclusive=True):
            found = self._compare(project, expected)
            self._write_pointer(project, None, damaged=found is POINTER_UNREADABLE)
            self._report(project, None, None)

    def current_chain(self, project):
        with self._locked(project, exclusive=False):
            found = self._pointer_or_damage(project)
            if found is POINTER_UNREADABLE:
                self._fail(project, 'pointer_damaged')
            return found

    def _head(self, project, chain, kind):
        require_canonical_id(chain)
        with self._locked(project, exclusive=False):
            if kind == slots.ItemKind.HIGH_WATER:
                self._item(project, chain, slots.ItemKind.COUNTER)
            return self._item(project, chain, kind).value

    def counter_head(self, project, chain):
        return self._head(project, chain, slots.ItemKind.COUNTER)

    def rendered_through(self, project, chain):
        return self._head(project, chain, slots.ItemKind.HIGH_WATER)

    def ledger_matches(self, project, chain, ledger_fd):
        require_canonical_id(project); require_canonical_id(chain)
        current = slots.file_identity(ledger_fd)
        with self._locked(project, exclusive=False):
            recorded = self._item(project, chain, slots.ItemKind.COUNTER).ledger_identity
            key = (project, chain)
            if (recorded.uuid is None) != (current.uuid is None) and key not in self._identity_warned:
                self._identity_warned.add(key)
                strings.report_diagnostic(SealIdentityOneSided(project, chain))
            return slots.identities_match(recorded, current)

    def is_broken(self, project, chain):
        require_canonical_id(chain)
        with self._locked(project, exclusive=False):
            return self.folder.mark_exists(project, chain)

    def folder_state(self, project):
        state = self.folder.folder_state(project)
        if state == 'unreadable':
            self._report(project, None, 'folder_unusable')
        return state

    def chains(self, project):
        try:
            return self.folder.chains(project)
        except OSError as error:
            self._fail(project, 'folder_unusable', error=error)

    def _keys(self):
        try:
            return self.folder.usable_keys()
        except OSError:
            return ()

    def _key(self, key_id):
        return next((key for key in self._keys() if key.key_id == key_id), None)

    def seal_record(self, row, prev_mac, key_id):
        require_canonical_id(row.project_id); require_canonical_id(row.chain_id)
        _number(row.number)
        if key_id is None:
            if row.number != 0:
                raise ValueError('key_required_after_zero')
            keys = self._keys()
            key = keys[-1] if keys else None
        else:
            key = self._key(key_id)
        if key is None:
            return None
        record = {'format': 'adt-approval-record-v1', 'project_id': row.project_id,
                  'chain_id': row.chain_id, 'number': row.number,
                  'event': {'kind': row.kind, 'payload': row.payload, 'at_ms': row.at_ms},
                  'prev_mac': prev_mac, 'key_id': key.key_id}
        return jcs_encode({'record': record, 'mac': _key_mac(key, _RECORD_CONTEXT, jcs_encode(record))})

    def open_record(self, data):
        try:
            value = _object(data)
            record = value['record']
            if (set(value) != {'record', 'mac'} or type(record) is not dict or
                    set(record) != _RECORD_FIELDS or record['format'] != 'adt-approval-record-v1'):
                raise ValueError('record_shape')
            _number(record['number'])
            event = record['event']
            if (type(event) is not dict or set(event) != {'kind', 'payload', 'at_ms'} or
                    type(event['kind']) is not str or type(event['payload']) is not dict or
                    type(event['at_ms']) is not int or
                    record['prev_mac'] is not None and not _hex(record['prev_mac'], 64)):
                raise ValueError('event_shape')
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise RecordInvalid('format') from None
        try:
            project = require_canonical_id(record['project_id'])
            chain = require_canonical_id(record['chain_id'])
        except ValueError:
            raise RecordInvalid('binding') from None
        key = self._key(record['key_id'])
        if key is None:
            raise RecordInvalid('key_unknown')
        digest = value['mac']
        if not _hex(digest, 64) or not hmac.compare_digest(
                digest, _key_mac(key, _RECORD_CONTEXT, jcs_encode(record))):
            raise RecordInvalid('mac')
        try:
            broken = self.folder.mark_exists(project, chain)
        except OSError:
            raise RecordInvalid('mark_unreadable') from None
        if broken:
            raise RecordInvalid('chain_broken')
        return SealedRecord(project, chain, record['number'], event,
                            record['prev_mac'], record['key_id'], digest)

    def read_record(self, project, chain, number):
        require_canonical_id(project); require_canonical_id(chain); _number(number)
        data = self.folder.open_record_file(project, chain, number)
        if data is None:
            return None
        record = self.open_record(data)
        if (record.project_id, record.chain_id, record.number) != (project, chain, number):
            raise RecordUnreadable(number, 'name_mismatch')
        return record

    def _chain_key(self, project, chain):
        try:
            data = self.folder.open_record_file(project, chain, 0)
            if data is not None:
                record = self.open_record(data)
                if (record.project_id, record.chain_id, record.number) != (project, chain, 0):
                    return None
                return self._key(record.key_id)
        except RecordInvalid as exc:
            if exc.cause == 'mark_unreadable':
                self._fail(project, 'folder_unusable')
            return None
        except RecordUnreadable:
            return None
        keys = self._keys()
        return keys[-1] if keys else None

    def reserve(self, project, chain, *, kind, subject, payload, transaction, declared_kinds=None):
        require_canonical_id(project); require_canonical_id(chain)
        parsed = _object(payload)  # Validate before even opening the project's lock.
        with self._locked(project, exclusive=True):
            found = self._pointer_or_damage(project)
            if found is POINTER_UNREADABLE:
                self._fail(project, 'pointer_damaged')
            if found is None:
                self._fail(project, 'pointer_unset')
            if found != chain:
                self._fail(project, 'ledger_chain_mismatch', found)
            state = self._item(project, chain, slots.ItemKind.COUNTER)
            self._item(project, chain, slots.ItemKind.HIGH_WATER)
            if self.folder.mark_exists(project, chain):
                raise ReserveRefused('chain_broken')
            previous = self.declarations.get(project)
            if previous is None or previous[0] != transaction:
                if not declared_kinds:
                    raise ReserveRefused('declaration_missing')
                declaration, position = declared_kinds, 0
            else:
                if declared_kinds is not None:
                    raise ReserveRefused('declaration_unexpected')
                _, declaration, position = previous
            if position >= len(declaration):
                raise ReserveRefused('past_declaration')
            if kind != declaration[position]:
                raise ReserveRefused('kind_mismatch')
            number = state.value + 1
            entry = {'format': 'adt-approval-entry-v1', 'project_id': project, 'chain_id': chain,
                     'number': number, 'kind': kind, 'subject': subject, 'payload': parsed,
                     'runtime_instance_id': self.runtime_instance_id, 'transaction': transaction,
                     'position': position, 'declared_kinds': list(declaration) if position == 0 else None}
            body = jcs_encode(entry)
            key = self._chain_key(project, chain)
            seal = {'key_id': key.key_id, 'mac': _key_mac(key, _ENTRY_CONTEXT, body)} if key else None
            data = jcs_encode({'entry': entry, 'sha256': hashlib.sha256(body).hexdigest(), 'seal': seal})
            try:
                self.folder.write_entry(project, chain, number, data)
                with self._open(project, chain + '.counter', write=True) as fd:
                    if not slots.identities_match(state.self_identity, slots.file_identity(fd)):
                        raise slots.CannotRecord(8)
                    # The project flock still protects the validated state above.
                    # A second read would repeat a damaged-slot diagnostic.
                    updated = replace(state, sequence=state.sequence + 1, value=number)
                    slots._write(fd, slots._slot(updated), slots.OFFSETS[updated.sequence % 2])
                    slots._sync(fd)
            except (OSError, slots.CannotRecord) as error:
                self._fail(project, 'counter_unusable', chain, error=error)
            self.declarations[project] = (transaction, tuple(declaration), position + 1)
            mismatch_projects = self._mismatch_projects
            if project in mismatch_projects:
                self._report(project, chain, 'recorded')
            return number

    def _entry(self, data, project, chain, number):
        if data is None:
            return UnreadableEntry(number, 'missing')
        try:
            value = _object(data)
            entry = value['entry']
            if (set(value) != {'entry', 'sha256', 'seal'} or type(entry) is not dict or
                    set(entry) != _ENTRY_FIELDS or entry['format'] != 'adt-approval-entry-v1'):
                raise ValueError('entry_shape')
            body = jcs_encode(entry)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return UnreadableEntry(number, 'parse')
        if not _hex(value['sha256'], 64) or not hmac.compare_digest(
                value['sha256'], hashlib.sha256(body).hexdigest()):
            return UnreadableEntry(number, 'checksum')
        if (entry['project_id'], entry['chain_id'], entry['number']) != (project, chain, number):
            return UnreadableEntry(number, 'binding')
        try:
            require_canonical_id(entry['runtime_instance_id'])
            _number(entry['number']); _number(entry['position'])
            if (type(entry['transaction']) is not int or type(entry['kind']) is not str or
                    type(entry['subject']) is not str or type(entry['payload']) is not dict):
                raise ValueError('entry_types')
            declaration = entry['declared_kinds']
            if entry['position'] == 0:
                if type(declaration) is not list or not declaration or any(type(k) is not str for k in declaration):
                    raise ValueError('declaration_shape')
                declaration = tuple(declaration)
            elif declaration is not None:
                raise ValueError('declaration_position')
        except ValueError:
            return UnreadableEntry(number, 'parse')
        seal = value['seal']
        if seal is not None:
            if type(seal) is not dict or set(seal) != {'key_id', 'mac'}:
                return UnreadableEntry(number, 'seal')
            key = self._key(seal['key_id'])
            if key is None or not _hex(seal['mac'], 64) or not hmac.compare_digest(
                    seal['mac'], _key_mac(key, _ENTRY_CONTEXT, body)):
                return UnreadableEntry(number, 'seal')
        return ReservationEntry(number, entry['kind'], entry['subject'], entry['payload'],
                                entry['runtime_instance_id'], entry['transaction'], entry['position'],
                                seal is not None, declaration)

    def reservations_above(self, project, chain, after):
        require_canonical_id(project); require_canonical_id(chain); _number(after)
        with self._locked(project, exclusive=False):
            head = self._item(project, chain, slots.ItemKind.COUNTER).value
            data = dict(self.folder.read_entries(project, chain, after=after, through=head))
            return tuple(self._entry(data.get(n), project, chain, n) for n in range(after + 1, head + 1))

    def mark_broken(self, project, chain, *, cause, number):
        require_canonical_id(project); require_canonical_id(chain); _number(number)
        if cause not in ('counter_behind', 'record_conflict'):
            raise ValueError('unknown_broken_cause')
        with self._locked(project, exclusive=True):
            if self.folder.mark_exists(project, chain):
                return
            mark = {'format': 'adt-approval-broken-v1', 'project_id': project, 'chain_id': chain,
                    'cause': cause, 'number': number, 'at_ms': self.clock_ms()}
            self.folder.write_mark(project, chain, jcs_encode(
                {'mark': mark, 'sha256': hashlib.sha256(jcs_encode(mark)).hexdigest()}))

    def drain(self, project, chain, *, through):
        require_canonical_id(project); require_canonical_id(chain); _number(through)
        if self.renderer is None:
            raise RuntimeError('seal_renderer_not_attached')
        return self.renderer.drain(project, chain, through=through)

    def committed(self, project, chain):
        require_canonical_id(project); require_canonical_id(chain)
        if self.renderer is not None:
            self.renderer.committed(project, chain)


def _expected(value, *, sentinel):
    if value is None or sentinel and value is POINTER_UNREADABLE:
        return
    require_canonical_id(value)


def current_chain(project_id):
    return _installed().current_chain(project_id)


def counter_head(project_id, chain_id):
    return _installed().counter_head(project_id, chain_id)


def rendered_through(project_id, chain_id):
    return _installed().rendered_through(project_id, chain_id)


def is_broken(project_id, chain_id):
    return _installed().is_broken(project_id, chain_id)


def ledger_matches(project_id, chain_id, ledger_fd):
    return _installed().ledger_matches(project_id, chain_id, ledger_fd)


def runtime_instance_id():
    return _installed().runtime_instance_id


def folder_state(project_id):
    return _installed().folder_state(project_id)


def chains(project_id):
    return _installed().chains(project_id)


def set_current(project_id, chain_id, *, ledger_fd, expected, bind):
    return _installed().set_current(project_id, chain_id, ledger_fd=ledger_fd,
                                    expected=expected, bind=bind)


def return_current(project_id, chain_id, *, expected):
    return _installed().return_current(project_id, chain_id, expected=expected)


def clear_current(project_id, *, expected):
    return _installed().clear_current(project_id, expected=expected)


def seal_record(row, prev_mac, key_id):
    return _installed().seal_record(row, prev_mac, key_id)


def open_record(data):
    return _installed().open_record(data)


def read_record(project_id, chain_id, number):
    return _installed().read_record(project_id, chain_id, number)


def reserve(project_id, chain_id, *, kind, subject, payload, transaction, declared_kinds=None):
    return _installed().reserve(project_id, chain_id, kind=kind, subject=subject, payload=payload,
                                transaction=transaction, declared_kinds=declared_kinds)


def reservations_above(project_id, chain_id, after):
    return _installed().reservations_above(project_id, chain_id, after)


def mark_broken(project_id, chain_id, *, cause, number):
    return _installed().mark_broken(project_id, chain_id, cause=cause, number=number)


def drain(project_id, chain_id, *, through):
    return _installed().drain(project_id, chain_id, through=through)


def committed(project_id, chain_id):
    return _installed().committed(project_id, chain_id)
