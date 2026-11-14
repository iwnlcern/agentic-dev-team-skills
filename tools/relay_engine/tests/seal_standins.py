"""Explicit temporary ledgers and manually advanced hook clocks."""
from collections import deque
import os
from pathlib import Path
import tempfile
import threading

from relay_engine import store_layout
from relay_engine.seal_types import EventRow


def event_row(project_id, chain_id, number, kind='approve', payload=None, at_ms=40):
    return EventRow(project_id, chain_id, number, kind,
                    {} if payload is None else payload, at_ms)


class Ledger:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()
        self.trace = []

    def put(self, row):
        with self.lock:
            self.rows = [r for r in self.rows if (r.project_id, r.chain_id, r.number) !=
                         (row.project_id, row.chain_id, row.number)] + [row]

    def open_reader(self, root, project_id):
        return Reader(self, project_id)


class Reader:
    def __init__(self, ledger, project):
        self.ledger, self.project = ledger, project
        self.owner = threading.get_ident()
        self.in_transaction = False
        self.closed = False
        self.trace('open')

    def trace(self, operation):
        if operation != 'interrupt':
            assert self.owner == threading.get_ident(), operation
        self.ledger.trace.append((operation, self.owner, threading.get_ident()))

    def execute(self, statement):
        self.trace(statement)
        self.in_transaction = statement == 'BEGIN'
        return _Cursor()

    def close(self):
        self.trace('close'); self.closed = True

    def interrupt(self):
        self.trace('interrupt')


class _Cursor:
    def close(self):
        pass


def events_after(conn, project_id, chain_id, after_seq):
    if isinstance(conn, Reader):
        conn.trace('events_after')
        assert conn.in_transaction
        with conn.ledger.lock:
            return sorted((r for r in conn.ledger.rows if r.project_id == project_id and
                           r.chain_id == chain_id and r.number > after_seq), key=lambda r: r.number)
    cursor = conn.execute('SELECT id FROM projects')
    try:
        assert cursor.fetchall() == [(project_id,)]
        with conn.ledger.lock:
            return sorted((r for r in conn.ledger.rows if r.project_id == project_id and
                           r.chain_id == chain_id and r.number > after_seq), key=lambda r: r.number)
    finally:
        cursor.close()


class ManualWait:
    def __init__(self):
        self.condition = threading.Condition()
        self.calls = []
        self.pending = deque()

    def __call__(self, event, seconds):
        decision = threading.Event()
        with self.condition:
            self.calls.append(seconds)
            if event.is_set():
                self.condition.notify_all()
                return True
            self.pending.append(decision)
            self.condition.notify_all()
        # This helper merely delivers the event to the controlled waiter.
        def delivered():
            event.wait(); decision.set()
        helper = threading.Thread(target=delivered, daemon=True)
        helper.start()
        decision.wait()
        with self.condition:
            if decision in self.pending:
                self.pending.remove(decision)
            return event.is_set()

    def expire(self):
        with self.condition:
            assert self.condition.wait_for(lambda: self.pending, timeout=30)
            self.pending.popleft().set()

    def until_calls(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.calls) >= count, timeout=30)


class ManualTick:
    def __init__(self):
        self.condition = threading.Condition()
        self.steps = deque()
        self.calls = []

    def __call__(self, seconds):
        with self.condition:
            self.calls.append(seconds)
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.steps)
            return self.steps.popleft()

    def advance(self):
        with self.condition:
            self.steps.append(True); self.condition.notify_all()

    def finish(self):
        with self.condition:
            self.steps.append(False); self.condition.notify_all()

    def until_calls(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.calls) >= count, timeout=30)


class ManualClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, milliseconds):
        self.value += milliseconds


class TempStore:
    def __init__(self, project_id):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalogue = store_layout.open_catalogue(self.root)
        self.writer = store_layout.open_ledger(self.root, project_id, create=True)
        self.writer.execute('INSERT INTO projects(id,name,created_at) VALUES (?, ?, 1)',
                            (project_id, 'seal fixture')).close()
        self.writer.commit()
        self.fd = os.open(store_layout.ledger_path(self.root, project_id), os.O_RDONLY)
        self.ledger = Ledger()
        self.readers = []

    def put(self, row):
        self.ledger.put(row)

    def open_reader(self, root, project_id):
        reader = StoreReader(store_layout.open_ledger_readonly(root, project_id), self.ledger)
        self.readers.append(reader)
        return reader

    def close(self):
        os.close(self.fd)
        self.writer.close(); self.catalogue.close(); self.tmp.cleanup()


class StoreReader:
    def __init__(self, connection, ledger):
        self.connection, self.ledger = connection, ledger
        self.owner = threading.get_ident()
        self.closed = False
        self.trace = []

    def execute(self, *args):
        assert threading.get_ident() == self.owner
        self.trace.append((args[0], threading.get_ident()))
        return self.connection.execute(*args)

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def interrupt(self):
        self.trace.append(('interrupt', threading.get_ident()))
        self.connection.interrupt()

    def close(self):
        assert threading.get_ident() == self.owner
        self.trace.append(('close', threading.get_ident()))
        self.connection.close(); self.closed = True


class Refusal:
    def __init__(self, code='E_API_UNAVAILABLE', cause_kind='runtime_not_ready'):
        self.code, self.cause_kind = code, cause_kind


class Unavailable(Exception):
    pass


class OpenCondition:
    def __init__(self, kind, subkind, condition_key, item_id):
        self.kind, self.subkind = kind, subkind
        self.condition_key, self.item_id = condition_key, item_id


class QueueServiceStandIn:
    """Only the pinned queue methods; None selects no-project rows."""
    def __init__(self):
        self.items = {}
        self.calls = []
        self.notifications = 0
        self.behavior = {}
        self.before_raise = None
        self.before_clear = None

    def _answer(self, method, project):
        answer = self.behavior.get((method, project))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def run(self, operation: str, args, principal):
        assert operation == 'queue.raise' and principal.service == 'approval_seal'
        project = args['project_id']
        if self.before_raise is not None:
            self.before_raise(operation, args, principal)
        answer = self._answer('raise', project)
        if answer is not None:
            return answer
        key = (args['kind'], args['subkind'], project, args['condition_key'])
        for item in self.items.values():
            own = (item['kind'], item['subkind'], item['project_id'], item['condition_key'])
            if item['open'] and own == key:
                self.calls.append(('raise', item['item_id']))
                return {'item_id': item['item_id']}
        item_id = str(len(self.items) + 1)
        self.items[item_id] = dict(args, item_id=item_id, open=True)
        self.calls.append(('raise', item_id))
        self.notifications += 1
        return {'item_id': item_id}

    def clear_condition(self, principal, item_id, kind, subkind, project_id, condition_key):
        assert principal.service == 'approval_seal'
        if self.before_clear is not None:
            self.before_clear(principal, item_id, kind, subkind, project_id, condition_key)
        self.calls.append(('clear', item_id))
        answer = self._answer('clear', project_id)
        if answer is not None:
            return answer
        item = self.items[item_id]
        assert (item['kind'],item['subkind'],item['project_id'],item['condition_key']) == (kind,subkind,project_id,condition_key)
        item['open'] = False
        return {'outcome': 'cleared'}

    def open_condition_items(self, principal, project_id: str | None):
        assert principal.service == 'approval_seal'
        self.calls.append(('recover', project_id))
        answer = self._answer('recover', project_id)
        if answer is not None:
            return answer
        return [OpenCondition(i['kind'], i['subkind'], i['condition_key'], i['item_id'])
                for i in self.items.values() if i['open'] and i['project_id'] == project_id]


def seal_texts(cause: str, chain_id: str):
    return {'sentence': 'Approvals cannot be recorded: ' + cause,
            'subject': chain_id}


def backup_texts():
    return {'sentence': 'The approval folder backup exclusion could not be confirmed.'}


def backup_pins():
    return {'folder': 'approval_seal'}
