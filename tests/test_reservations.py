"""CPU-only tests for reservation interval calculations."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from app.reservations import normalized_window, peak_reserved_gpu  # noqa: E402


T0 = datetime(2026, 12, 1, 10, 0, tzinfo=timezone.utc)


class ReservationLogicTests(unittest.TestCase):
    def test_no_bookings(self):
        self.assertEqual(peak_reserved_gpu([], T0, T0 + timedelta(hours=2)), 0)

    def test_adjacent_bookings_are_not_added(self):
        rows = [(T0, T0 + timedelta(hours=1), 1),
                (T0 + timedelta(hours=1), T0 + timedelta(hours=2), 1)]
        self.assertEqual(peak_reserved_gpu(rows, T0, T0 + timedelta(hours=2)), 1)

    def test_true_overlap_is_added(self):
        rows = [(T0, T0 + timedelta(hours=2), 1),
                (T0 + timedelta(minutes=30), T0 + timedelta(hours=1), 1)]
        self.assertEqual(peak_reserved_gpu(rows, T0, T0 + timedelta(hours=2)), 2)

    def test_non_intersecting_bookings_ignored(self):
        rows = [(T0 - timedelta(hours=2), T0, 2),
                (T0 + timedelta(hours=2), T0 + timedelta(hours=3), 2)]
        self.assertEqual(peak_reserved_gpu(rows, T0, T0 + timedelta(hours=2)), 0)

    def test_multiple_nonconcurrent_bookings_do_not_overcount(self):
        rows = [(T0, T0 + timedelta(hours=1), 1),
                (T0 + timedelta(hours=1), T0 + timedelta(hours=2), 2)]
        self.assertEqual(peak_reserved_gpu(rows, T0, T0 + timedelta(hours=2)), 2)

    def test_timezone_conversion(self):
        from datetime import timezone as tz
        cet = tz(timedelta(hours=1))
        start, end = normalized_window(T0.astimezone(cet),
                                       (T0 + timedelta(hours=1)).astimezone(cet))
        self.assertEqual((start, end), (T0, T0 + timedelta(hours=1)))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(HTTPException) as exc:
            normalized_window(T0.replace(tzinfo=None), T0 + timedelta(hours=1))
        self.assertEqual(exc.exception.status_code, 422)

    def test_reversed_interval_rejected(self):
        with self.assertRaises(HTTPException) as exc:
            normalized_window(T0 + timedelta(hours=1), T0)
        self.assertEqual(exc.exception.status_code, 422)

    def test_calendar_window_limit(self):
        with self.assertRaises(HTTPException) as exc:
            normalized_window(T0, T0 + timedelta(days=32), calendar=True)
        self.assertEqual(exc.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
