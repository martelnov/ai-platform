"""Opt-in PostgreSQL integration tests for GPU reservations.

Run only against the isolated ai_reservations_test database.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from fastapi import HTTPException

from app import reservations


class ReservationDatabaseTests(unittest.TestCase):

    def setUp(self):
        dsn = os.environ.get("AI_RESERVATIONS_TEST_DSN")

        if not dsn:
            self.skipTest("Isolated test database not configured")

        params = conninfo_to_dict(dsn)

        if (
            params.get("dbname") != "ai_reservations_test"
            or params.get("user") != "ai_test"
            or params.get("host") != "127.0.0.1"
        ):
            self.fail("Refusing connection to non-test database")

        self.db = lambda: psycopg.connect(dsn)
        self.user_ids = []

        with self.db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT host_id, count(*)
                    FROM gpu_devices
                    WHERE enabled AND present
                    GROUP BY host_id
                """)
                self.assertEqual(len(cur.fetchall()), 1)

                cur.execute("""
                    SELECT count(*)
                    FROM gpu_devices
                    WHERE enabled AND present
                """)
                self.assertEqual(cur.fetchone()[0], 2)

                cur.execute("""
                    SELECT COALESCE(MAX(ends_at), now())
                    FROM reservations
                """)
                last_end = cur.fetchone()[0]

                self.start = max(
                    datetime.now(timezone.utc) + timedelta(days=30),
                    last_end + timedelta(days=1),
                )
                self.end = self.start + timedelta(hours=1)

                cur.execute("""
                    SELECT GREATEST(
                        COALESCE(MAX(linux_uid), 0),
                        COALESCE(MAX(linux_gid), 0),
                        30000
                    )
                    FROM users
                """)
                base = cur.fetchone()[0]

                tag = uuid4().hex[:12]

                for i in range(3):
                    uid = base + i + 1
                    cur.execute("""
                        INSERT INTO users
                            (username, linux_uid, linux_gid)
                        VALUES (%s, %s, %s)
                        RETURNING id
                    """, (f"res_it_{tag}_{i}", uid, uid))
                    self.user_ids.append(cur.fetchone()[0])

        self.addCleanup(self.cleanup_users)

        # Authentication is tested separately.
        # Here the request argument represents a verified user ID.
        self.router = reservations.make_reservations_router(
            lambda conn, request: {"user": {"id": request}}
        )

        p = patch.object(reservations, "db_connect", self.db)
        p.start()
        self.addCleanup(p.stop)

    def cleanup_users(self):
        with self.db() as conn:
            conn.execute(
                "DELETE FROM users WHERE id = ANY(%s)",
                (self.user_ids,),
            )

    def endpoint(self, method, path):
        return next(
            r.endpoint
            for r in self.router.routes
            if r.path == path and method in r.methods
        )

    def reserve(self, user_id, start, end, count=1):
        return self.endpoint("POST", "/v1/reservations")(
            user_id,
            reservations.ReservationCreate(
                starts_at=start,
                ends_at=end,
                gpu_count=count,
            ),
        )

    def own(self, user_id):
        return self.endpoint("GET", "/v1/reservations")(
            user_id
        )["reservations"]

    def test_reservation_lifecycle(self):
        a, b, c = self.user_ids

        first = self.reserve(a, self.start, self.end)
        second = self.reserve(b, self.start, self.end)

        self.assertEqual(first["gpu_count"], 1)
        self.assertEqual(second["gpu_count"], 1)

        with self.assertRaises(HTTPException) as error:
            self.reserve(c, self.start, self.end)
        self.assertEqual(error.exception.status_code, 409)

        cancel = self.endpoint(
            "DELETE", "/v1/reservations/{reservation_id}"
        )
        result = cancel(b, second["id"])
        self.assertEqual(result["status"], "cancelled")

        third = self.reserve(c, self.start, self.end)
        self.assertEqual(third["status"], "scheduled")

        with self.assertRaises(HTTPException) as error:
            cancel(b, third["id"])
        self.assertEqual(error.exception.status_code, 404)

        later = self.start + timedelta(hours=3)
        later_end = later + timedelta(hours=1)

        both = self.reserve(b, later, later_end, 2)
        self.assertEqual(both["gpu_count"], 2)

        availability = self.endpoint(
            "GET", "/v1/reservations/availability"
        )(b, later, later_end)

        self.assertEqual(availability["total_gpus"], 2)
        self.assertEqual(availability["available_gpus"], 0)

        timeline = self.endpoint(
            "GET", "/v1/reservations/timeline"
        )(b, later, later_end)

        self.assertEqual(
            sum(item["gpu_count"] for item in timeline["busy"]),
            2,
        )

        self.assertEqual(
            {r["id"] for r in self.own(a)},
            {first["id"]},
        )
        self.assertEqual(
            {r["id"] for r in self.own(b)},
            {second["id"], both["id"]},
        )
        self.assertEqual(
            {r["id"] for r in self.own(c)},
            {third["id"]},
        )

    def test_concurrent_reservations(self):
        a, b, c = self.user_ids

        self.reserve(a, self.start, self.end)
        barrier = Barrier(2)

        def attempt(user_id):
            barrier.wait(timeout=15)
            try:
                result = self.reserve(
                    user_id, self.start, self.end
                )
                return ("created", result["id"])
            except HTTPException as exc:
                return ("rejected", exc.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fb = pool.submit(attempt, b)
            fc = pool.submit(attempt, c)
            results = [
                fb.result(timeout=30),
                fc.result(timeout=30),
            ]

        self.assertEqual(
            sum(r[0] == "created" for r in results), 1
        )
        self.assertEqual(
            sum(r == ("rejected", 409) for r in results), 1
        )

        with self.db() as conn:
            rows = conn.execute("""
                SELECT gpu_count
                FROM reservations
                WHERE user_id = ANY(%s)
                  AND status IN ('scheduled', 'active')
            """, (self.user_ids,)).fetchall()

        self.assertEqual(sum(row[0] for row in rows), 2)


if __name__ == "__main__":
    unittest.main()
