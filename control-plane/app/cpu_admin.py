from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse


def make_cpu_admin_router(
    db_connect,
    require_admin,
    host_agent_request,
    templates,
):
    router = APIRouter(tags=["cpu-admin"])

    @router.get("/admin/cpu", response_class=HTMLResponse)
    def cpu_admin_page(request: Request):
        with db_connect() as conn:
            identity = require_admin(request, conn)

        response = templates.TemplateResponse(
            request=request,
            name="admin-cpu.html",
            context={"identity": identity},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/admin/cpu/inventory")
    def cpu_admin_inventory(request: Request):
        with db_connect() as conn:
            require_admin(request, conn)

        try:
            response = host_agent_request(
                {"op": "cpu_inventory_scan"},
                timeout=30,
                max_response_bytes=600000,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(
                503, "CPU host-agent is unavailable"
            ) from exc

        if not isinstance(response, dict) or response.get("ok") is not True:
            raise HTTPException(502, "CPU inventory scan failed")

        result = response.get("result")
        if not isinstance(result, dict):
            raise HTTPException(502, "Invalid CPU inventory response")

        return result

    return router
