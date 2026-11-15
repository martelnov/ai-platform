"""Static regression checks for the GPU reservations client-side filter."""
import unittest
from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "gateway" / "index.html"

class _FilterParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.options = []
        self.in_filter = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "select" and attrs.get("id") == "gpu-mine-filter":
            self.in_filter = True
        if tag == "option" and self.in_filter:
            self.options.append((attrs.get("value"), "selected" in attrs))

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_filter = False

class ReservationFilterPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.page = _FilterParser()
        cls.page.feed(cls.html)

    def test_choices_and_default(self):
        self.assertEqual(self.page.options, [
            ("valid", True), ("cancelled", False),
            ("past", False), ("all", False)
        ])
        for label in ("Platné", "Zrušené", "Minulé", "Všechno"):
            self.assertIn(label, self.html)
        self.assertEqual(self.page.ids.count("gpu-mine-filter"), 1)
        self.assertEqual(len(self.page.ids), len(set(self.page.ids)))

    def test_filter_uses_existing_api_and_updates_without_fetch(self):
        self.assertIn("function gpuVisibleMine(reservations, mode)", self.html)
        self.assertIn("function gpuMineCategory(item, nowMs)", self.html)
        self.assertIn("if (item.status === 'cancelled')", self.html)
        self.assertIn("if (item.status === 'completed')", self.html)
        self.assertIn("endMs > nowMs ? 'valid' : 'past'", self.html)
        self.assertIn("Date.now()", self.html)
        self.assertIn("gpuMineReservations = result.reservations;", self.html)
        self.assertIn("gpuMineFilter.addEventListener('change', gpuRefreshMineView);", self.html)
        self.assertIn("gpuMineReservations.length", self.html)
        self.assertIn("gpuLoadMine()", self.html)

if __name__ == "__main__":
    unittest.main()
