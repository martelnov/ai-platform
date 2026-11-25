from __future__ import annotations

import hmac
import math
import re
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StrictBool


CPUSET_RE = re.compile(r"^[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*$")


class ProfileResources(BaseModel):
    enabled: StrictBool
    cpuset_cpus: str = ""
    cpu_limit: float
    memory_limit_gib: float


def _single_host_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM hosts ORDER BY id")
        rows = cur.fetchall()

    if len(rows) != 1:
        raise HTTPException(
            503, "CPU administration currently requires one host"
        )

    return int(rows[0][0])


def _check_csrf(request, identity, csrf_token):
    supplied = request.headers.get("X-AI-Platform-CSRF", "")
    expected = csrf_token(identity["login"], "cpu-admin")

    if not isinstance(supplied, str) or not hmac.compare_digest(
        supplied, expected
    ):
        raise HTTPException(403, "Invalid CPU administration CSRF token")


def _parse_cpuset(value, online):
    if value == "":
        return None

    if not CPUSET_RE.fullmatch(value):
        raise HTTPException(422, "Invalid CPU list syntax")

    result = set()

    for part in value.split(","):
        bounds = part.split("-")
        start = int(bounds[0])
        end = int(bounds[-1])

        if end < start or end - start > 65535:
            raise HTTPException(422, "Invalid CPU range")

        values = set(range(start, end + 1))

        if result.intersection(values):
            raise HTTPException(422, "Duplicate CPU numbers")

        result.update(values)

    if not result.issubset(online):
        raise HTTPException(
            422, "CPU list contains unavailable logical CPUs"
        )

    return ",".join(str(cpu) for cpu in sorted(result))


def _agent_result(host_agent_request):
    try:
        response = host_agent_request(
            {"op": "cpu_inventory_scan"},
            timeout=30,
            max_response_bytes=600000,
        )
    except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
        raise HTTPException(503, "CPU host-agent is unavailable") from exc

    if not isinstance(response, dict) or response.get("ok") is not True:
        raise HTTPException(502, "CPU host-agent operation failed")

    result = response.get("result")

    if not isinstance(result, dict):
        raise HTTPException(502, "Invalid CPU inventory response")

    cpus = result.get("cpus")

    if not isinstance(cpus, list):
        raise HTTPException(502, "Invalid CPU topology")

    online = {
        item["cpu"]
        for item in cpus
        if isinstance(item, dict)
        and type(item.get("cpu")) is int
        and item.get("online") is True
    }

    if not online:
        raise HTTPException(502, "No online CPUs reported")

    return result, online


def make_cpu_admin_router(
    db_connect: Callable,
    require_admin: Callable,
    host_agent_request: Callable,
    templates,
    csrf_token: Callable,
):
    router = APIRouter(tags=["cpu-admin"])

    @router.get("/admin/cpu", response_class=HTMLResponse)
    def cpu_admin_page(request: Request):
        with db_connect() as conn:
            identity = require_admin(request, conn)

        response = templates.TemplateResponse(
            request=request,
            name="admin-cpu.html",
            context={
                "identity": identity,
                "cpu_csrf": csrf_token(identity["login"], "cpu-admin"),
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/admin/cpu/inventory")
    def cpu_admin_inventory(request: Request):
        with db_connect() as conn:
            require_admin(request, conn)

        result, _ = _agent_result(host_agent_request)
        return result

    @router.get("/admin/cpu/resources")
    def cpu_admin_resources(request: Request):
        with db_connect() as conn:
            require_admin(request, conn)
            host_id = _single_host_id(conn)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        a.code,
                        a.name,
                        ap.id,
                        ap.name,
                        ap.cpu_limit,
                        ap.memory_limit_bytes,
                        ap.gpu_min,
                        ap.gpu_max,
                        ap.enabled,
                        hpr.enabled,
                        hpr.cpuset_cpus,
                        hpr.cpu_limit,
                        hpr.memory_limit_bytes
                    FROM application_profiles ap
                    JOIN applications a ON a.id = ap.application_id
                    LEFT JOIN host_application_profile_resources hpr
                      ON hpr.host_id = %s
                     AND hpr.application_profile_id = ap.id
                    ORDER BY a.code, ap.id
                    """,
                    (host_id,),
                )
                rows = cur.fetchall()

        return {
            "host_id": host_id,
            "profiles": [
                {
                    "application": row[0],
                    "application_name": row[1],
                    "profile_id": row[2],
                    "profile": row[3],
                    "default_cpu_limit": float(row[4]),
                    "default_memory_bytes": row[5],
                    "gpu_min": row[6],
                    "gpu_max": row[7],
                    "profile_enabled": row[8],
                    "override_enabled": bool(row[9]) if row[9] is not None else False,
                    "cpuset_cpus": row[10] or "",
                    "cpu_limit": float(row[11]) if row[11] is not None else float(row[4]),
                    "memory_limit_bytes": row[12] if row[12] is not None else row[5],
                }
                for row in rows
            ],
        }

    @router.post("/admin/cpu/resources/{profile_id}")
    def cpu_admin_save(
        request: Request,
        profile_id: int,
        body: ProfileResources,
    ):
        if profile_id <= 0:
            raise HTTPException(422, "Invalid profile ID")

        if (
            not math.isfinite(body.cpu_limit)
            or body.cpu_limit <= 0
            or body.cpu_limit > 256
        ):
            raise HTTPException(422, "Invalid CPU limit")

        if (
            not math.isfinite(body.memory_limit_gib)
            or body.memory_limit_gib < 0.25
            or body.memory_limit_gib > 1048576
        ):
            raise HTTPException(422, "Invalid RAM limit")

        memory_bytes = int(body.memory_limit_gib * 1024 ** 3)

        with db_connect() as conn:
            identity = require_admin(request, conn)
            _check_csrf(request, identity, csrf_token)

            host_id = _single_host_id(conn)

            _, online = _agent_result(host_agent_request)
            cpuset = _parse_cpuset(body.cpuset_cpus.strip(), online)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM application_profiles
                    WHERE id = %s
                    """,
                    (profile_id,),
                )
                if cur.fetchone() is None:
                    raise HTTPException(404, "Application profile not found")

                cur.execute(
                    """
                    INSERT INTO host_application_profile_resources (
                        host_id,
                        application_profile_id,
                        cpuset_cpus,
                        cpu_limit,
                        memory_limit_bytes,
                        enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (host_id, application_profile_id)
                    DO UPDATE SET
                        cpuset_cpus = EXCLUDED.cpuset_cpus,
                        cpu_limit = EXCLUDED.cpu_limit,
                        memory_limit_bytes = EXCLUDED.memory_limit_bytes,
                        enabled = EXCLUDED.enabled,
                        updated_at = now()
                    """,
                    (
                        host_id,
                        profile_id,
                        cpuset,
                        body.cpu_limit,
                        memory_bytes,
                        body.enabled,
                    ),
                )

        return {"ok": True, "profile_id": profile_id}

    return router
