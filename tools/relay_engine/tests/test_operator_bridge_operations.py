import unittest

from relay_engine import api_operator_bridge, api_platform


class PageOperationRegistryTests(unittest.TestCase):
    def test_page_allows_registered_dismiss_all(self):
        names = api_operator_bridge._PAGE_OPERATIONS
        self.assertIn(
            "queue.dismiss-all", names, f"page allowlist has {sorted(names)}"
        )
        self.assertNotIn("queue.dismiss_all", names)

    def test_every_page_operation_is_registered(self):
        registry = api_platform.build_platform().registry
        for name in sorted(api_operator_bridge._PAGE_OPERATIONS):
            with self.subTest(operation=name):
                self.assertIsNotNone(registry.operation(name), name)

    def test_every_page_read_is_registered(self):
        registry = api_platform.build_platform().registry
        for name in sorted(api_operator_bridge._PAGE_READS):
            with self.subTest(operation=name):
                self.assertIsNotNone(registry.operation(name), name)
