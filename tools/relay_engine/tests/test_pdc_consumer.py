"""Task 8's consumer case: pdc's real inbox sweeper reads the engine's mirror
header lines (PR-3 G-E2).

pdc is a private local project, so this module runs on the release check's mac
leg only (tools/release-check.py, DD-v3-tests § 3.6); a direct run on a host
without pdc's checkout skips and names the path it looked for.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from relay_engine import header

# The one source of the sweeper's path; ADT_PDC_SWEEPER names another machine's checkout.
PDC_SWEEPER = Path(os.environ.get("ADT_PDC_SWEEPER",
                                  "/Users/jack/Programming/pdc/tools/inbox-sweep.py"))


class TestPdcConsumer(unittest.TestCase):
    def sweep(self, seat, reply_parent):
        with tempfile.TemporaryDirectory() as name:
            team = Path(name, "master", "relays", "t")
            team.mkdir(parents=True)
            inbound = "IMPL-pair-planner-20261104-010000.md"
            (team / inbound).write_text("\n".join(header.render_mirror_header(
                "t.planner", [seat], ["v.orchestrator-planner"], None)) + "\n\nplease act\n")
            (team / "IMPL-pair-implementer-20261104-010100.md").write_text("\n".join(
                header.render_mirror_header(seat, ["t.planner"], [],
                                            "t/" + inbound if reply_parent else None))
                + "\n\ndone\n")
            result = subprocess.run(
                [sys.executable, "-I", str(PDC_SWEEPER), seat, "--all", "--root", name],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, cwd=name)
        rows = [line.split() for line in result.stdout.splitlines() if inbound in line]
        return result.returncode, rows

    @unittest.skipUnless(PDC_SWEEPER.is_file(), "pdc checkout absent: %s" % PDC_SWEEPER)
    def test_pdc_sweeper_consumed(self):
        code, rows = self.sweep("t.implementer", reply_parent=True)
        self.assertEqual((code, [row[1] for row in rows]), (0, ["consumed"]))
        code, rows = self.sweep("t.implementer", reply_parent=False)  # control
        self.assertEqual((code, [row[1] for row in rows]), (1, ["UNREAD"]))


if __name__ == "__main__":
    unittest.main()
