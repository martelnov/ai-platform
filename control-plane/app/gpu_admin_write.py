"""Admin-only GPU registration and capacity-aware enable/disable operations.

Single-host / capacity-only version. The 'gpu_uuid' field uses a generated,
non-hardware inventory key if no persistent card identifier is available.
Changing the physical card in a PCI slot cannot be detected reliably from BDF
alone; the administrator explicitly confirms registration/enablement.

No driver management, GPU assignment, or physical removal is performed here.
"""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone
from uuid import uuid4
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool

# The reservation API uses the same transaction-scoped lock for booking writes.
from app.reservations import _ADVISORY_LOCK_CLASS, _ADVISORY_LOCK_OBJECT

_BDF = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


class GPURegistration(BaseModel):
    pci_bus_id: str


class GPUEnabledChange(BaseModel):
    enabled: StrictBool


class GPURetiredChange(BaseModel):
    retired: StrictBool


def check_csrf(request: Request, identity: dict, csrf_token: Callable) -> None:
    supplied = request.headers.get("X-AI-Platform-CSRF", "")
    expected = csrf_token(identity["login"], "gpu-admin")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "Invalid GPU administration CSRF token")


def _as_bdf(value: str) -> str:
    if not isinstance(value, str) or not _BDF.fullmatch(value):
        raise HTTPException(422, "Invalid PCI address")
    return value.lower()


def scan_device(host_agent_request: Callable, agent_result: Callable, bdf: str) -> dict:
    """Require one exactly matching *detected* display adapter."""
    result = agent_result(host_agent_request, {"op": "gpu_inventory_scan"})
    devices = result.get("devices")
    if not isinstance(devices, list):
        raise HTTPException(502, "Invalid GPU inventory response")
    matches = [d for d in devices if isinstance(d, dict) and
               isinstance(d.get("pci_bus_id"), str) and
               d["pci_bus_id"].lower() == bdf]
    if not matches:
        raise HTTPException(409, "The selected adapter is not currently detected")
    if len(matches) != 1:
        raise HTTPException(502, "Duplicate PCI addresses returned by scanner")
    return matches[0]


def _safe_int(value, *, min_value=0):
    return value if type(value) is int and value >= min_value else None


def _pcie_generation(speed):
    if not isinstance(speed, str):
        return None
    match = re.match(r"^([\d.]+)\s*GT/s", speed)
    return {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5,
            "64.0": 6}.get(match.group(1)) if match else None


def _peak_future(bookings, now: datetime) -> int:
    """Maximum concurrent active + scheduled reservations from now forward.

    End events must precede starts at a shared boundary (half-open windows).
    Do not sum non-overlapping reservations.
    """
    events = []
    for start, end, count in bookings:
        if end <= now:
            continue
        start = max(start, now)
        if start < end:
            events.extend(((start, int(count)), (end, -int(count))))
    events.sort(key=lambda item: (item[0], item[1]))
    used = maximum = 0
    for _, delta in events:
        used += delta
        maximum = max(maximum, used)
    return maximum


def _lock_reservations(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                    (_ADVISORY_LOCK_CLASS, _ADVISORY_LOCK_OBJECT))


def _guard_capacity_reduction(conn, host_id: int) -> None:
    """Refuse capacity reductions that overbook active or future reservations.

    Caller must hold the same transaction advisory lock as reservation writers.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM gpu_devices
               WHERE host_id = %s AND enabled = true AND present = true""",
            (host_id,),
        )
        current_capacity = int(cur.fetchone()[0])
        proposed_capacity = max(0, current_capacity - 1)
        cur.execute(
            """SELECT starts_at, ends_at, gpu_count
               FROM reservations
               WHERE status IN ('scheduled', 'active') AND ends_at > now()"""
        )
        peak = _peak_future(cur.fetchall(), datetime.now(timezone.utc))
    if peak > proposed_capacity:
        raise HTTPException(
            409,
            f"Reducing GPU capacity to {proposed_capacity} would overbook "
            f"existing reservations requiring {peak}",
        )


def add_gpu_write_routes(
    router: APIRouter,
    db_connect: Callable,
    require_admin: Callable,
    host_agent_request: Callable,
    csrf_token: Callable,
    single_host_id: Callable,
    agent_result: Callable,
):
    """Attach mutation endpoints to the existing admin-verified GPU router."""

    @router.post("/admin/gpus/register", status_code=201)
    def register_gpu(request: Request, body: GPURegistration):
        bdf = _as_bdf(body.pci_bus_id)
        with db_connect() as conn:
            identity = require_admin(request, conn)
            check_csrf(request, identity, csrf_token)
            # Scan *after* admin + CSRF checks; read-only but privileged.
            device = scan_device(host_agent_request, agent_result, bdf)
            name = device.get("pci_name")
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(502, "GPU scanner did not supply a model name")
            name = name.strip()[:500]
            gpu_data = device.get("amdgpu") or {}
            pcie = device.get("pcie") or {}
            vram = _safe_int(gpu_data.get("vram_total_bytes"), min_value=1) if isinstance(gpu_data, dict) else None
            numa = _safe_int(device.get("numa_node"))
            width = _safe_int(pcie.get("max_link_width"), min_value=1) if isinstance(pcie, dict) else None
            generation = _pcie_generation(pcie.get("max_link_speed")) if isinstance(pcie, dict) else None

            _lock_reservations(conn)
            host_id = single_host_id(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id FROM gpu_devices
                       WHERE host_id = %s AND lower(pci_bus_id) = %s
                         AND retired_at IS NULL""",
                    (host_id, bdf),
                )
                if cur.fetchone() is not None:
                    raise HTTPException(409, "Adapter already registered at this PCI address")
                # Never derive a durable GPU identity from a potentially changing PCI slot.
                # This is a unique, persistent *inventory key*, not a physical UUID.
                inventory_uuid = "inventory:" + uuid4().hex
                cur.execute(
                    """INSERT INTO gpu_devices
                       (host_id, gpu_uuid, pci_bus_id, name, memory_total_bytes,
                        numa_node, pcie_generation, pcie_width, enabled, present)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, true)
                       RETURNING id""",
                    (host_id, inventory_uuid, bdf, name, vram, numa,
                     generation, width),
                )
                gpu_id = cur.fetchone()[0]
        return {"id": gpu_id, "host_id": host_id, "pci_bus_id": bdf,
                "name": name, "enabled": False, "present": True,
                "identity_note": "Inventory UUID is not a hardware identity"}

    @router.post("/admin/gpus/{gpu_id}/enabled")
    def set_gpu_enabled(request: Request, gpu_id: int, body: GPUEnabledChange):
        if gpu_id <= 0:
            raise HTTPException(422, "Invalid GPU id")
        with db_connect() as conn:
            identity = require_admin(request, conn)
            check_csrf(request, identity, csrf_token)
            _lock_reservations(conn)
            host_id = single_host_id(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, enabled, present, pci_bus_id, retired_at
                       FROM gpu_devices WHERE id = %s AND host_id = %s FOR UPDATE""",
                    (gpu_id, host_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, "GPU registration not found")
                _, old_enabled, present, old_bdf, retired_at = row
                if body.enabled and retired_at is not None:
                    raise HTTPException(409, "Retired GPU must be restored before enabling")
                if bool(old_enabled) == body.enabled:
                    return {"id": gpu_id, "enabled": bool(old_enabled),
                            "present": bool(present), "changed": False}

                if body.enabled:
                    if not isinstance(old_bdf, str) or not _BDF.fullmatch(old_bdf):
                        raise HTTPException(409, "Registered GPU has no valid PCI address")
                    # Require a fresh scan; BDF matching is provisional, admin confirms action.
                    scan_device(host_agent_request, agent_result, old_bdf.lower())
                    cur.execute(
                        """UPDATE gpu_devices
                           SET enabled = true, present = true,
                               last_seen_at = now(), updated_at = now()
                           WHERE id = %s""", (gpu_id,),
                    )
                    return {"id": gpu_id, "enabled": True,
                            "present": True, "changed": True}

                # Capacity reduction must serialize with reservation creation.
                if present:
                    _guard_capacity_reduction(conn, host_id)
                cur.execute(
                    "UPDATE gpu_devices SET enabled = false, updated_at = now() WHERE id = %s",
                    (gpu_id,),
                )
        return {"id": gpu_id, "enabled": False,
                "present": bool(present), "changed": True}

    @router.post("/admin/gpus/{gpu_id}/retired")
    def set_gpu_retired(request: Request, gpu_id: int, body: GPURetiredChange):
        """Soft-retire or restore a GPU; never erase its registry identity.

        Retirement forces enabled=false atomically. Restoration keeps the GPU
        disabled even if physically present. Both actions are audit-logged.
        """
        if gpu_id <= 0:
            raise HTTPException(422, "Invalid GPU id")
        with db_connect() as conn:
            identity = require_admin(request, conn)
            check_csrf(request, identity, csrf_token)
            _lock_reservations(conn)
            host_id = single_host_id(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, enabled, present, pci_bus_id, retired_at
                       FROM gpu_devices WHERE id = %s AND host_id = %s FOR UPDATE""",
                    (gpu_id, host_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, "GPU registration not found")
                _, was_enabled, present, old_bdf, retired_at = row
                currently_retired = retired_at is not None
                if currently_retired == body.retired:
                    return {
                        "id": gpu_id, "retired": currently_retired,
                        "enabled": bool(was_enabled), "retired_at": retired_at,
                        "changed": False,
                    }

                if body.retired:
                    # Same advisory lock as booking writes, held until commit.
                    if was_enabled and present:
                        _guard_capacity_reduction(conn, host_id)
                    cur.execute(
                        """UPDATE gpu_devices
                           SET enabled = false, retired_at = now(), updated_at = now()
                           WHERE id = %s RETURNING retired_at""",
                        (gpu_id,),
                    )
                    new_retired_at = cur.fetchone()[0]
                    action = "retire"
                else:
                    # Do not restore the old card on top of a newly registered
                    # replacement in the same PCI slot. Both keep their history.
                    if isinstance(old_bdf, str):
                        cur.execute(
                            """SELECT id FROM gpu_devices
                               WHERE host_id = %s AND lower(pci_bus_id) = %s
                                 AND retired_at IS NULL AND id <> %s""",
                            (host_id, old_bdf.lower(), gpu_id),
                        )
                        if cur.fetchone() is not None:
                            raise HTTPException(
                                409, "PCI slot belongs to another active GPU registration"
                            )
                    cur.execute(
                        """UPDATE gpu_devices
                           SET enabled = false, retired_at = NULL, updated_at = now()
                           WHERE id = %s""",
                        (gpu_id,),
                    )
                    new_retired_at = None
                    action = "restore"

                cur.execute(
                    """INSERT INTO gpu_device_events (gpu_id, action, admin_login)
                       VALUES (%s, %s, %s)""",
                    (gpu_id, action, identity["login"]),
                )
        return {
            "id": gpu_id, "retired": body.retired,
            "enabled": False, "retired_at": new_retired_at,
            "changed": True,
        }
