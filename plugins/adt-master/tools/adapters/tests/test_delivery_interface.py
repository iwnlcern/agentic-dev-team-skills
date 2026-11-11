"""The Adapter delivery contract (DD-v3-adapter r8 section 4.2)."""
import ast
import inspect
import types
import unittest

import tools.adapters.delivery_interface as delivery_interface


class DeliveryInterfaceTests(unittest.TestCase):
    def assert_intervals(self, module):
        self.assertEqual(module.DOORBELL_POLL_INTERVAL_S, 5.0)
        self.assertEqual(module.NOT_HEARD_AFTER_S, 10.0)

    def assert_reasons(self, module):
        self.assertEqual(
            module.OFF_REASONS,
            (
                "window-gone",
                "stood-down",
                "project-closed",
                "replaced",
                "version-refused",
                "session-not-current",
                "new-conversation",
            ),
        )
        self.assertNotIn("not-heard", module.OFF_REASONS)
        self.assertEqual(module.STATES, ("armed", "unavailable", "off"))
        self.assertEqual(module.ARMED_REASONS, ("backlog",))
        self.assertEqual(module.UNAVAILABLE_REASONS, ("monitor-tool",))

    def assert_no_imports(self, source):
        imports = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertEqual(imports, [])

    def test_poll_interval_is_the_exported_constant(self):
        self.assert_intervals(delivery_interface)
        changed = types.SimpleNamespace(**vars(delivery_interface))
        changed.DOORBELL_POLL_INTERVAL_S = 4.0
        with self.assertRaises(AssertionError):
            self.assert_intervals(changed)

    def test_reason_lists_match_r8(self):
        self.assert_reasons(delivery_interface)
        changed = types.SimpleNamespace(**vars(delivery_interface))
        changed.OFF_REASONS += ("not-heard",)
        with self.assertRaises(AssertionError):
            self.assert_reasons(changed)

    def test_interface_module_imports_nothing(self):
        source = inspect.getsource(delivery_interface)
        self.assert_no_imports(source)
        with self.assertRaises(AssertionError):
            self.assert_no_imports(source + "\nimport os\n")


if __name__ == "__main__":
    unittest.main()
