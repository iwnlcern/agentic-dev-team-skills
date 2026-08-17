import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from relay_engine.rules import relay_order_key


class TestOrderKey(unittest.TestCase):
    def test_stat_fallback_orders_by_path_not_mtime(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            a, b = root / "a/same.md", root / "b/same.md"
            a.parent.mkdir()
            b.parent.mkdir()
            b.touch()
            a.touch()
            os.utime(b, (1, 1))
            os.utime(a, (2, 2))
            ordered = sorted([b, a], key=lambda p: relay_order_key(p, root))
            self.assertEqual(
                [p.relative_to(root).as_posix() for p in ordered],
                ["a/same.md", "b/same.md"],
            )

    def test_timestamped_names_keep_tier_zero(self):
        key = relay_order_key(Path("x/SITREP-planner-20260816-212040.md"))
        self.assertEqual(key[0], 0)
