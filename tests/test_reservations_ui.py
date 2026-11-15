"""Dependency-free checks for the GPU calendar embedded in the launcher."""

import unittest
from html.parser import HTMLParser
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "gateway" / "index.html"


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.scripts = []
        self.script_src = []
        self._inside_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script":
            self._inside_script = True
            if "src" in attrs:
                self.script_src.append(attrs["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._inside_script = False

    def handle_data(self, data):
        if self._inside_script:
            self.scripts.append(data)


class ReservationCalendarPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.page = _Page()
        cls.page.feed(cls.html)
        cls.js = "\n".join(cls.page.scripts)

    def test_controls_have_unique_identifiers(self):
        self.assertEqual(len(self.page.ids), len(set(self.page.ids)))
        expected = {
            "gpu-open", "gpu-close", "gpu-panel", "gpu-calendar",
            "gpu-prev", "gpu-next", "gpu-today", "gpu-reload",
            "gpu-form", "gpu-start", "gpu-end", "gpu-count",
            "gpu-create", "gpu-availability", "gpu-mine",
            "gpu-mine-message", "gpu-create-message",
        }
        self.assertTrue(expected.issubset(set(self.page.ids)))
        self.assertIn('id="gpu-panel"', self.html)
        self.assertIn('aria-live="polite"', self.html)

    def test_views_and_existing_salamander_controls(self):
        for view in ("day", "week", "month"):
            self.assertIn('data-gpu-view="' + view + '"', self.html)
        for old in ("salamander-start", "salamander-open", "salamander-stop"):
            self.assertIn('id="' + old + '"', self.html)
        self.assertIn("changeSalamander('start')", self.js)
        self.assertIn("changeSalamander('stop')", self.js)

    def test_only_fixed_reservation_apis_and_action_headers(self):
        for path in (
            "/api/reservations/timeline?",
            "/api/reservations/availability?",
            "'/api/reservations'",
            "'/api/reservations/'",
        ):
            self.assertIn(path, self.js)
        self.assertIn("'reservation-create'", self.js)
        self.assertIn("'reservation-cancel'", self.js)
        self.assertIn("gpuSelectedWindow(true)", self.js)
        self.assertIn("30 * GPU_DAY", self.js)  # Avoid >31-day calendar API calls.

    def test_untrusted_api_content_is_not_html_inserted(self):
        self.assertEqual(self.page.script_src, [])
        self.assertNotIn("innerHTML", self.js)
        self.assertNotIn("eval(", self.js)
        self.assertIn("gpuElement('small'", self.js)
        self.assertIn("textContent", self.js)


if __name__ == "__main__":
    unittest.main()
