"""Read-only GPU inventory and diagnostics for the existing admin UI.

This module intentionally does not register, disable, or delete GPUs. It uses
existing Control Plane authentication and an allowlisted host-agent protocol.
"""

from __future__ import annotations

import re
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse


BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
DIAGNOSTIC_TOOLS = frozenset(
    ("auto", "all", "lspci", "nvidia-smi", "amd-smi", "rocm-smi")
)
MAX_DIAGNOSTIC_RESPONSE = 600_000


def reconcile_inventory(detected: list[dict], registered: list[dict]) -> list[dict]:
    """Merge the scan with the single-host DB inventory without writing to it.

    Matching by PCI address is *provisional*: replacing a card in the same
    slot or moving one between slots cannot be established from BDF alone.
    """
    active_by_bdf: dict[str, list[dict]] = {}
    retired_by_bdf: dict[str, list[dict]] = {}
    for record in registered:
        bdf = record.get("pci_bus_id")
        if not isinstance(bdf, str):
            continue
        directory = (retired_by_bdf if record.get("retired_at") is not None
                     else active_by_bdf)
        directory.setdefault(bdf.lower(), []).append(record)

    result = []
    matched_ids = set()
    for device in detected:
        bdf = device.get("pci_bus_id")
        if not isinstance(bdf, str) or not BDF_RE.fullmatch(bdf):
            raise ValueError("Scanner returned invalid PCI address")
        key = bdf.lower()
        matches = active_by_bdf.get(key, [])
        if len(matches) > 1:
            raise ValueError("Multiple registered GPUs use the same PCI address")
        if matches:
            record = matches[0]
            scan_status = "registered"
            match_basis = "pci_bus_id_provisional"
        else:
            # A scan of a retired PCI slot is NOT a second GPU. Display the
            # observed adapter together with the most recent retired record.
            # This is *only* a slot association, NEVER proof that the retired
            # physical adapter is still installed. The admin must choose to
            # restore the original record or register a replacement.
            retired = retired_by_bdf.get(key, [])
            record = max(retired, key=lambda item: item["id"]) if retired else None
            scan_status = "retired" if record else "new"
            match_basis = "retired_slot_provisional" if record else None
        if record is not None:
            matched_ids.add(record["id"])
        result.append({
            "scan_status": scan_status,
            "match_basis": match_basis,
            "detected": device,
            "registered": record,
        })

    for record in registered:
        if record["id"] not in matched_ids:
            result.append({
                "scan_status": "retired" if record.get("retired_at") is not None else "missing",
                "match_basis": None,
                "detected": None,
                "registered": record,
            })
    return result


def _registered_for_host(conn, host_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, host_id, gpu_uuid, pci_bus_id, name,
                      enabled, present, last_seen_at, retired_at
               FROM gpu_devices WHERE host_id = %s ORDER BY id""",
            (host_id,),
        )
        rows = cur.fetchall()
    keys = (
        "id", "host_id", "gpu_uuid", "pci_bus_id", "name", "enabled",
        "present", "last_seen_at", "retired_at",
    )
    return [dict(zip(keys, row)) for row in rows]


def _single_host_id(conn) -> int:
    """Reservation v1 is explicitly single-host. Do not merge other hosts."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM hosts ORDER BY id")
        hosts = cur.fetchall()
    if len(hosts) != 1:
        raise HTTPException(503, "GPU administration currently requires one host")
    return int(hosts[0][0])


def _agent_result(host_agent_request: Callable, payload: dict, *, diagnostics=False):
    try:
        if diagnostics:
            response = host_agent_request(
                payload, timeout=60, max_response_bytes=MAX_DIAGNOSTIC_RESPONSE
            )
        else:
            response = host_agent_request(payload, timeout=60)
    except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
        raise HTTPException(503, "GPU host-agent is unavailable") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise HTTPException(502, "GPU host-agent operation failed")
    result = response.get("result")
    if not isinstance(result, dict):
        raise HTTPException(502, "Invalid GPU host-agent response")
    return result


def make_gpu_admin_router(
    db_connect: Callable,
    require_admin: Callable,
    host_agent_request: Callable,
    templates,
    csrf_token: Callable | None = None,
) -> APIRouter:
    """Factory to avoid circular imports with the existing app.main module."""
    router = APIRouter(tags=["gpu-admin"])

    @router.get("/admin/gpus", response_class=HTMLResponse)
    def gpu_admin_page(request: Request):
        with db_connect() as conn:
            identity = require_admin(request, conn)
        response = templates.TemplateResponse(
            request=request,
            name="admin-gpus.html",
            context={"identity": identity,
                     "gpu_csrf": csrf_token(identity["login"], "gpu-admin")
                     if csrf_token is not None else ""},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/admin/gpus/inventory")
    def gpu_admin_inventory(request: Request):
        # Authenticate before invoking the privileged host-agent.
        with db_connect() as conn:
            require_admin(request, conn)
            host_id = _single_host_id(conn)
            registered = _registered_for_host(conn, host_id)

        scan = _agent_result(host_agent_request, {"op": "gpu_inventory_scan"})
        detected = scan.get("devices")
        if not isinstance(detected, list) or not all(
            isinstance(item, dict) for item in detected
        ):
            raise HTTPException(502, "Invalid GPU scan data")
        try:
            devices = reconcile_inventory(detected, registered)
        except ValueError as exc:
            raise HTTPException(502, "Invalid GPU scan data") from exc

        return {
            "schema_version": 1,
            "host_id": host_id,
            "host": scan.get("host"),
            "scanned_at": scan.get("scanned_at"),
            "tools_available": scan.get("tools_available", {}),
            "devices": devices,
            "note": "PCI associations are provisional. A detected adapter in a retired slot may be the original card or a replacement; confirm physical identity before restoring or registering.",
        }

    @router.get("/admin/gpus/diagnostics")
    def gpu_admin_diagnostics(request: Request, pci_bus_id: str, tool: str = "auto"):
        # Reject attacker-controlled command names and malformed PCI addresses.
        if not BDF_RE.fullmatch(pci_bus_id) or tool not in DIAGNOSTIC_TOOLS:
            raise HTTPException(422, "Invalid PCI address or diagnostic tool")
        with db_connect() as conn:
            require_admin(request, conn)
        result = _agent_result(
            host_agent_request,
            {"op": "gpu_diagnostics", "pci_bus_id": pci_bus_id, "tool": tool},
            diagnostics=True,
        )
        if result.get("pci_bus_id", "").lower() != pci_bus_id.lower():
            raise HTTPException(502, "Diagnostic response PCI address mismatch")
        if not isinstance(result.get("results"), list):
            raise HTTPException(502, "Invalid GPU diagnostic response")
        return result

    if csrf_token is not None:
        from app.gpu_admin_write import add_gpu_write_routes
        add_gpu_write_routes(
            router, db_connect, require_admin, host_agent_request,
            csrf_token, _single_host_id, _agent_result,
        )
    return router
