"""Installed approval-seal service; all item operations use the project flock."""
from contextlib import contextmanager
import os
from pathlib import Path
import pwd
import secrets
import sys
import threading

from . import seal_slots as slots, strings
from .seal_files import SealFolder
from .seal_types import (POINTER_UNREADABLE, PointerUnreadable, PointerMoved,
                         LedgerRebindRefused, SealUnusable, SealReport,
                         SealIdentityOneSided, require_canonical_id)

_service = None
_install_lock = threading.Lock()
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


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

    def attach_renderer(self, renderer):
        if self._renderer_attached:
            raise RuntimeError('seal_renderer_already_attached')
        self.renderer, self._renderer_attached = renderer, True

    def _report(self, project, chain, cause):
        self.report(SealReport(project, chain, cause, self.clock_ms()))

    def _fail(self, project, cause, chain=None, *, report=True):
        if report:
            self._report(project, chain, cause)
        raise SealUnusable(cause, chain) from None

    @contextmanager
    def _locked(self, project, *, exclusive, create=False):
        path = self.folder._project(project)
        try:
            if not create and not path.is_dir():
                raise OSError('missing_project')
            # The folder must support recording, even for a read-only call.
            if path.exists() and not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise OSError('unusable_project')
            with self.folder.lock(project, exclusive=exclusive):
                yield
        except OSError:
            self._fail(project, 'folder_unusable')

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
        except (OSError, slots.CannotRecord):
            found = self._pointer_or_damage(project)
            if found is None:
                self._report(project, None, 'pointer_unset')
            elif found is POINTER_UNREADABLE:
                self._report(project, None, 'pointer_damaged')
            self._fail(project, cause, chain, report=found == chain)

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
        except (OSError, slots.CannotRecord):
            self._fail(project, cause, chain)

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
                    state = slots.read_slots(fd, kind=slots.ItemKind.POINTER,
                                             binding=(project,), ledger_identity=None)
                    slots.write_next(fd, state, value=0,
                                     chain=chain.encode('ascii') if chain is not None else None)
        except FileNotFoundError:
            if not create:
                self._fail(project, 'pointer_unset')
            self._new_pointer(project, chain)
        except slots.CannotRecord:
            self._fail(project, 'pointer_damaged')

    def set_current(self, project, chain, *, ledger_fd, expected, bind):
        require_canonical_id(project); require_canonical_id(chain)
        _expected(expected, sentinel=True)
        if bind not in ('first', 'rebind'):
            raise ValueError('invalid_bind')
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
                        except slots.CannotRecord:
                            self._fail(project, 'counter_unusable', chain)
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
            return self._item(project, chain, kind).value

    def counter_head(self, project, chain):
        return self._head(project, chain, slots.ItemKind.COUNTER)

    def rendered_through(self, project, chain):
        return self._head(project, chain, slots.ItemKind.HIGH_WATER)

    def ledger_matches(self, project, chain, ledger_fd):
        require_canonical_id(chain)
        with self._locked(project, exclusive=False):
            recorded = self._item(project, chain, slots.ItemKind.COUNTER).ledger_identity
            current = slots.file_identity(ledger_fd)
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
        return self.folder.folder_state(project)

    def chains(self, project):
        try:
            return self.folder.chains(project)
        except OSError:
            self._fail(project, 'folder_unusable')


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
