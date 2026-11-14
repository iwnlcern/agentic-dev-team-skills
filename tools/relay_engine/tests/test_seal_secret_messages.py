"""Observe outward seal messages over all slice fixtures, on their temporary roots."""
from contextlib import ExitStack
import functools
import importlib
import inspect
import os
from pathlib import Path
import re
import threading
import unittest
from unittest import mock
import weakref


MODULES = ('seal_types', 'seal_slots', 'seal_files', 'approval_seal',
           'seal_renderer', 'seal_worker')
FIXTURES = ('test_seal_slots', 'test_seal_files', 'test_approval_seal',
            'test_seal_reserve', 'test_seal_renderer', 'test_seal_worker',
            'test_principal_construction')
FAULTS = ('SealUnusable', 'ReserveRefused', 'PointerMoved', 'LedgerRebindRefused',
          'RecordInvalid', 'RecordUnreadable', 'SealSlotDamaged',
          'SealIdentityOneSided', 'SealReportDropped', 'SealCalleeFailed')


class SealSecretMessagesTests(unittest.TestCase):
    def test_secret_never_in_messages_every_fixture(self):
        modules = {
            'seal_types': importlib.import_module('relay_engine.seal_types'),
            'seal_slots': importlib.import_module('relay_engine.seal_slots'),
            'seal_files': importlib.import_module('relay_engine.seal_files'),
            'approval_seal': importlib.import_module('relay_engine.approval_seal'),
            'seal_renderer': importlib.import_module('relay_engine.seal_renderer'),
            'seal_worker': importlib.import_module('relay_engine.seal_worker'),
        }
        types = modules['seal_types']
        files = modules['seal_files']
        strings = importlib.import_module('relay_engine.strings')
        stand = importlib.import_module('relay_engine.tests.seal_standins')
        messages, secrets, directories = [], [], []
        constructors = {name: 0 for name in FAULTS}
        lock = threading.Lock()
        depth = threading.local()

        def record(*values):
            with lock:
                messages.extend(str(value) for value in values)

        def boundary(call):
            @functools.wraps(call)
            def wrapped(*args, **kwargs):
                before = getattr(depth, 'value', 0)
                depth.value = before + 1
                try:
                    return call(*args, **kwargs)
                except BaseException as exc:
                    if before == 0:
                        record(type(exc).__name__, str(exc), repr(exc))
                    raise
                finally:
                    depth.value = before
            return wrapped

        def constructor(name, original):
            @functools.wraps(original)
            def wrapped(obj, *args, **kwargs):
                original(obj, *args, **kwargs)
                with lock:
                    constructors[name] += 1
                record(str(obj), repr(obj), repr(args))
            return wrapped

        class RecordedKeys(weakref.WeakKeyDictionary):
            def __setitem__(self, key, value):
                with lock:
                    secrets.append(bytes(value))
                super().__setitem__(key, value)

        with ExitStack() as patches:
            # Patch methods rather than replacing classes: isinstance and signatures stay valid.
            for name in MODULES[1:]:
                module = modules[name]
                for attr, value in list(vars(module).items()):
                    if attr.startswith('_') or getattr(value, '__module__', None) != module.__name__:
                        continue
                    if inspect.isfunction(value):
                        patches.enter_context(mock.patch.object(module, attr, boundary(value)))
                    elif inspect.isclass(value):
                        for method, descriptor in list(vars(value).items()):
                            if method.startswith('_') and method != '__init__':
                                continue
                            if isinstance(descriptor, staticmethod):
                                replacement = staticmethod(boundary(descriptor.__func__))
                            elif isinstance(descriptor, classmethod):
                                replacement = classmethod(boundary(descriptor.__func__))
                            elif inspect.isfunction(descriptor):
                                replacement = boundary(descriptor)
                            else:
                                continue
                            patches.enter_context(mock.patch.object(value, method, replacement))

            for name in FAULTS:
                cls = getattr(types, name)
                patches.enter_context(mock.patch.object(cls, '__init__', constructor(name, cls.__init__)))

            original_report = types.SealReport.__init__
            def report(obj, *args, **kwargs):
                original_report(obj, *args, **kwargs)
                record(repr(obj))
            patches.enter_context(mock.patch.object(types.SealReport, '__init__', report))

            original_folder = files.SealFolder.__init__
            @functools.wraps(original_folder)
            def folder(obj, *args, **kwargs):
                original_folder(obj, *args, **kwargs)
                with lock:
                    directories.append(str(obj.root / 'keys'))
            patches.enter_context(mock.patch.object(files.SealFolder, '__init__', folder))
            patches.enter_context(mock.patch.object(files, '_KEY_BYTES', RecordedKeys()))

            original_diagnostic = strings.report_diagnostic
            def diagnostic(*args, **kwargs):
                record(repr(args), repr(kwargs))
                return original_diagnostic(*args, **kwargs)
            patches.enter_context(mock.patch.object(strings, 'report_diagnostic', diagnostic))

            def queue_method(original):
                @functools.wraps(original)
                def wrapped(obj, *args, **kwargs):
                    record(repr(args), repr(kwargs))
                    return original(obj, *args, **kwargs)
                return wrapped
            for name in ('run', 'clear_condition', 'open_condition_items'):
                original = getattr(stand.QueueServiceStandIn, name)
                patches.enter_context(mock.patch.object(stand.QueueServiceStandIn, name, queue_method(original)))

            original_hook = threading.excepthook
            def thread_exception(args):
                if args.thread.name.startswith(('seal-', 'approval-seal')):
                    record(args.exc_type.__name__, str(args.exc_value), repr(args.exc_value))
                original_hook(args)
            patches.enter_context(mock.patch.object(threading, 'excepthook', thread_exception))

            suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(
                'relay_engine.tests.' + name) for name in FIXTURES)
            expected = suite.countTestCases()
            result = unittest.TestResult()
            suite.run(result)

        self.assertTrue(result.wasSuccessful(), repr(result.errors + result.failures))
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.testsRun, expected)
        self.assertTrue(secrets, 'fixtures must create or read keys')
        self.assertTrue(directories, 'fixtures must construct seal roots')
        for text in messages:
            for secret in secrets:
                self.assertNotIn(repr(secret), text, 'key bytes in outward message')
                self.assertNotIn(secret.hex(), text, 'key hex in outward message')
            for directory in directories:
                self.assertNotIn(directory, text, 'key directory in outward message')
            self.assertNotIn(os.sep + 'keys' + os.sep, text, 'key path in outward message')
        self.assertEqual([name for name, count in constructors.items() if not count], [],
                         'missing fault constructor coverage')
        for module in modules.values():
            source = Path(module.__file__).read_text()
            self.assertIsNone(re.search(r'logging|print\(|sys\.stdout|sys\.stderr|warnings\.', source),
                              'unlisted seal message exit in ' + module.__name__)


if __name__ == '__main__':
    unittest.main()
