"""Small single-host GPU reservation service for the user gateway.

This module reserves *capacity in time*, not concrete PCIe devices. GPU
assignment and enforcement in workload containers are intentionally separate.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictInt

from app.db import db_connect


# Shared by all gateway API workers. A transaction-scoped PostgreSQL advisory
# lock serializes creation/cancellation to prevent concurrent overbooking.
_ADVISORY_LOCK_CLASS = 104912
_ADVISORY_LOCK_OBJECT = 1
_MAX_CALENDAR_WINDOW = timedelta(days=31)


class ReservationCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime
    gpu_count: StrictInt


def normalized_window(starts_at: datetime, ends_at: datetime, *, calendar=False):
    """Require unambiguous time zones and a positive half-open interval."""
    if (
        starts_at.tzinfo is None
        or ends_at.tzinfo is None
        or starts_at.utcoffset() is None
        or ends_at.utcoffset() is None
    ):
        raise HTTPException(422, "Time zone offset is required")
    start = starts_at.astimezone(timezone.utc)
    end = ends_at.astimezone(timezone.utc)
    if start >= end:
        raise HTTPException(422, "ends_at must be after starts_at")
    if calendar and end - start > _MAX_CALENDAR_WINDOW:
        raise HTTPException(422, "Calendar window cannot exceed 31 days")
    return start, end


def peak_reserved_gpu(rows, start: datetime, end: datetime) -> int:
    """Maximum simultaneous reservations within [start, end).

    Each row contains (starts_at, ends_at, gpu_count). Adjacent intervals
    do not overlap. Summing all intersecting rows would be incorrect.
    """
    events = []
    for booked_start, booked_end, gpu_count in rows:
        left = max(start, booked_start)
        right = min(end, booked_end)
        if left < right:
            events.append((left, gpu_count))
            events.append((right, -gpu_count))
    # Ending bookings release capacity before starting bookings at same time.
    events.sort(key=lambda item: (item[0], item[1]))
    used = highest = 0
    for _, difference in events:
        used += difference
        highest = max(highest, used)
    return highest


def _gpu_capacity(conn) -> int:
    """V1 deliberately supports only one host with enabled, present GPUs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT host_id, count(*)
            FROM gpu_devices
            WHERE enabled = true AND present = true
            GROUP BY host_id
            ORDER BY host_id
            """
        )
        hosts = cur.fetchall()
    if len(hosts) > 1:
        raise HTTPException(503, "Multi-host reservations are not supported yet")
    return int(hosts[0][1]) if hosts else 0


def _bookings(conn, start: datetime, end: datetime):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT starts_at, ends_at, gpu_count
            FROM reservations
            WHERE status IN ('scheduled', 'active')
              AND starts_at < %s
              AND ends_at > %s
            """,
            (end, start),
        )
        return cur.fetchall()


def _reservation_dict(row):
    return {
        "id": row[0],
        "starts_at": row[1],
        "ends_at": row[2],
        "gpu_count": row[3],
        "status": row[4],
    }


def make_reservations_router(resolve_identity: Callable) -> APIRouter:
    """Inject the existing HMAC-verified connector identity resolver."""
    router = APIRouter(prefix="/v1/reservations", tags=["reservations"])

    @router.get("")
    def own_reservations(request: Request):
        with db_connect() as conn:
            identity = resolve_identity(conn, request)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, starts_at, ends_at, gpu_count, status
                    FROM reservations
                    WHERE user_id = %s
                      AND ends_at >= now() - interval '30 days'
                    ORDER BY starts_at
                    LIMIT 500
                    """,
                    (identity["user"]["id"],),
                )
                rows = cur.fetchall()
        return {"reservations": [_reservation_dict(row) for row in rows]}

    @router.get("/availability")
    def availability(request: Request, starts_at: datetime, ends_at: datetime):
        start, end = normalized_window(starts_at, ends_at, calendar=True)
        with db_connect() as conn:
            resolve_identity(conn, request)
            capacity = _gpu_capacity(conn)
            booked = _bookings(conn, start, end)
        used = peak_reserved_gpu(booked, start, end)
        return {
            "starts_at": start,
            "ends_at": end,
            "total_gpus": capacity,
            "available_gpus": max(capacity - used, 0),
        }

    @router.get("/timeline")
    def calendar_timeline(request: Request, starts_at: datetime, ends_at: datetime):
        start, end = normalized_window(starts_at, ends_at, calendar=True)
        with db_connect() as conn:
            resolve_identity(conn, request)
            capacity = _gpu_capacity(conn)
            booked = _bookings(conn, start, end)
        # No usernames, reservation IDs, or other users' private details.
        return {
            "total_gpus": capacity,
            "busy": [
                {
                    "starts_at": max(start, booked_start),
                    "ends_at": min(end, booked_end),
                    "gpu_count": count,
                }
                for booked_start, booked_end, count in booked
            ],
        }

    @router.post("", status_code=201)
    def create_reservation(request: Request, body: ReservationCreate):
        start, end = normalized_window(body.starts_at, body.ends_at)
        if body.gpu_count not in (1, 2):
            raise HTTPException(422, "Only one or two GPUs can be reserved")
        if start <= datetime.now(timezone.utc):
            raise HTTPException(422, "Reservation must start in the future")

        with db_connect() as conn:
            identity = resolve_identity(conn, request)
            user_id = identity["user"]["id"]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_ADVISORY_LOCK_CLASS, _ADVISORY_LOCK_OBJECT),
                )
                capacity = _gpu_capacity(conn)
                if body.gpu_count > capacity:
                    raise HTTPException(409, "Insufficient registered GPU capacity")

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM reservations
                        WHERE user_id = %s
                          AND status IN ('scheduled', 'active')
                          AND starts_at < %s AND ends_at > %s
                    )
                    """,
                    (user_id, end, start),
                )
                if cur.fetchone()[0]:
                    raise HTTPException(409, "You already have a reservation in this interval")

                used = peak_reserved_gpu(_bookings(conn, start, end), start, end)
                if used + body.gpu_count > capacity:
                    raise HTTPException(409, "Requested GPU capacity is unavailable")

                cur.execute(
                    """
                    INSERT INTO reservations (user_id, starts_at, ends_at, gpu_count)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, starts_at, ends_at, gpu_count, status
                    """,
                    (user_id, start, end, body.gpu_count),
                )
                result = _reservation_dict(cur.fetchone())
        return result

    @router.delete("/{reservation_id}")
    def cancel_reservation(request: Request, reservation_id: int):
        with db_connect() as conn:
            identity = resolve_identity(conn, request)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_ADVISORY_LOCK_CLASS, _ADVISORY_LOCK_OBJECT),
                )
                cur.execute(
                    """
                    SELECT status, starts_at
                    FROM reservations
                    WHERE id = %s AND user_id = %s
                    FOR UPDATE
                    """,
                    (reservation_id, identity["user"]["id"]),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, "Reservation not found")
                if row[0] != "scheduled" or row[1] <= datetime.now(timezone.utc):
                    raise HTTPException(409, "Only future scheduled reservations can be cancelled")
                cur.execute(
                    """
                    UPDATE reservations
                    SET status = 'cancelled', updated_at = now()
                    WHERE id = %s
                    """,
                    (reservation_id,),
                )
        return {"id": reservation_id, "status": "cancelled"}

    return router
