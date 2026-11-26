"""Static checks for the three packaged Console page files."""

from pathlib import Path
import re
import unittest

from relay_engine import version


TOOLS = Path(__file__).resolve().parents[2]
PAGE = TOOLS / "relay_engine" / "console_page"
NAMES = ("index.html", "page.css", "page.js")


class TestConsolePageFiles(unittest.TestCase):
    def test_files_exist_and_rostered(self):
        for name in NAMES:
            self.assertTrue((PAGE / name).is_file(), name)
            self.assertIn(f"relay_engine/console_page/{name}", version.ROSTER)
        self.assertTrue(version.fingerprint(TOOLS))

    def test_no_inline_script_or_style(self):
        html = (PAGE / "index.html").read_text()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)|\bstyle\s*=|<style\b")

    def test_index_links_only_own_files(self):
        html = (PAGE / "index.html").read_text()
        self.assertCountEqual(re.findall(r'(?:src|href)="([^"]+)"', html), ("page.js", "page.css", "data:,"))

    def test_no_outside_url(self):
        for name in NAMES:
            self.assertNotRegex((PAGE / name).read_text(), r"https?:|['\x22]//")

    def test_no_html_sinks(self):
        js = (PAGE / "page.js").read_text()
        self.assertNotRegex(js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function")

    def test_no_storage(self):
        js = (PAGE / "page.js").read_text()
        self.assertNotRegex(js, r"localStorage|sessionStorage|indexedDB|document\.cookie")
