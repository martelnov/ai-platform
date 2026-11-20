"""Unit tests for read-only GPU administration endpoints.

No external test runner or HTTPX dependency is needed.
"""

import importlib.util
import unittest
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "control-plane" / "app" / "gpu_admin.py"
TEMPLATES = ROOT / "control-plane" / "app" / "templates"
if not SOURCE.exists():
    SOURCE = ROOT / "app" / "gpu_admin.py"
    TEMPLATES = ROOT / "app" / "templates"

spec = importlib.util.spec_from_file_location("gpu_admin_for_tests", SOURCE)
gpu_admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu_admin)

DETECTED = [
    {"pci_bus_id": "0000:08:00.0", "pci_name": "SM750", "driver_in_use": "sm750fb",
     "pcie": {"max_link_width": 1, "current_link_width": 1}},
    {"pci_bus_id": "0000:09:00.0", "pci_name": "RX 460", "driver_in_use": None,
     "pcie": {"max_link_width": 8, "current_link_width": 1}},
]

DB_RECORDS = [
    (1, 1, "amd:host-1:pci-0000:09:00.0", "0000:09:00.0", "RX 460", True, True, None, None),
    (2, 1, "old-card", "0000:0a:00.0", "Removed card", False, False, None, None),
]


class FakeConnection:
    def __init__(self, test):
        self.test = test

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return FakeCursor(self.test)


class FakeCursor:
    def __init__(self, test):
        self.test = test
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.test.queries.append((query, params))
        if not query.lstrip().upper().startswith("SELECT"):
            raise AssertionError("Admin scan tried to write to DB")
        if "FROM hosts" in query:
            self.rows = self.test.hosts
        elif "FROM gpu_devices" in query:
            self.rows = self.test.records
        else:
            raise AssertionError("Unexpected query " + query)

    def fetchall(self):
        return self.rows


class GPUAdminTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [(1,)]
        self.records = DB_RECORDS
        self.queries = []
        self.calls = []
        self.invalid_scan = None

        def require_admin(request, conn):
            if request.headers.get("tailscale-user-login") != "admin@example.invalid":
                raise HTTPException(403, "Administrator access required")
            return {"login": "admin@example.invalid", "name": "Admin"}

        def agent(payload, **kwargs):
            self.calls.append((payload, kwargs))
            if payload["op"] == "gpu_inventory_scan":
                return {"ok": True, "result": {
                    "schema_version": 1, "host": "ailab", "scanned_at": "2026-11-15T20:37:43Z",
                    "devices": DETECTED if self.invalid_scan is None else self.invalid_scan,
                    "tools_available": {"lspci": True, "nvidia-smi": False},
                }}
            return {"ok": True, "result": {
                "pci_bus_id": payload["pci_bus_id"],
                "results": [{"tool": "lspci", "status": "ok", "stdout": "PCIe x1"}],
            }}

        self.router = gpu_admin.make_gpu_admin_router(
            lambda: FakeConnection(self),
            require_admin,
            agent,
            Jinja2Templates(directory=str(TEMPLATES)),
        )

    def request(self, authorized=True):
        headers = [(b"tailscale-user-login", b"admin@example.invalid")] if authorized else []
        return Request({"type": "http", "method": "GET", "path": "/admin/gpus", "headers": headers})

    def endpoint(self, path):
        return next(r.endpoint for r in self.router.routes if r.path == path)

    def test_requires_admin_for_page_scan_diagnostics(self):
        request = self.request(authorized=False)
        for path, args in (
            ("/admin/gpus", ()),
            ("/admin/gpus/inventory", ()),
            ("/admin/gpus/diagnostics", ("0000:09:00.0", "auto")),
        ):
            with self.subTest(path=path), self.assertRaises(HTTPException) as err:
                self.endpoint(path)(request, *args)
            self.assertEqual(err.exception.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_inventory_classifies_new_registered_missing(self):
        payload = self.endpoint("/admin/gpus/inventory")(self.request())
        self.assertEqual(payload["host_id"], 1)
        self.assertEqual([d["scan_status"] for d in payload["devices"]],
                         ["new", "registered", "missing"])
        self.assertTrue(payload["devices"][1]["registered"]["enabled"])
        self.assertIsNone(payload["devices"][0]["registered"])
        self.assertEqual(payload["devices"][1]["match_basis"], "pci_bus_id_provisional")
        self.assertEqual(self.calls[0][0], {"op": "gpu_inventory_scan"})
        self.assertTrue(all(q.lstrip().upper().startswith("SELECT") for q, _ in self.queries))

    def test_single_host_only(self):
        self.hosts = [(1,), (2,)]
        with self.assertRaises(HTTPException) as err:
            self.endpoint("/admin/gpus/inventory")(self.request())
        self.assertEqual(err.exception.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_bad_scanner_data_rejected(self):
        self.invalid_scan = [{"pci_bus_id": "bad"}]
        with self.assertRaises(HTTPException) as err:
            self.endpoint("/admin/gpus/inventory")(self.request())
        self.assertEqual(err.exception.status_code, 502)

    def test_diagnostics_validate_bdf_and_tool(self):
        endpoint = self.endpoint("/admin/gpus/diagnostics")
        for bdf, tool in (("bad", "auto"), ("0000:09:00.0", "rm")):
            with self.subTest(bdf=bdf, tool=tool), self.assertRaises(HTTPException) as err:
                endpoint(self.request(), bdf, tool)
            self.assertEqual(err.exception.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_diagnostics_reaches_allowlisted_agent(self):
        result = self.endpoint("/admin/gpus/diagnostics")(
            self.request(), "0000:09:00.0", "lspci"
        )
        self.assertEqual(result["results"][0]["stdout"], "PCIe x1")
        self.assertEqual(self.calls[0][0], {
            "op": "gpu_diagnostics", "pci_bus_id": "0000:09:00.0", "tool": "lspci"
        })
        self.assertEqual(self.calls[0][1]["max_response_bytes"], 600_000)

    def test_page_has_safe_rendering(self):
        response = self.endpoint("/admin/gpus")(self.request())
        html = response.body.decode("utf-8")
        self.assertIn("/admin/gpus/inventory", html)
        self.assertIn("/admin/gpus/diagnostics?", html)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_reconciliation_rejects_duplicate_registered_bdf(self):
        records = [
            {"id": 1, "pci_bus_id": "0000:09:00.0"},
            {"id": 2, "pci_bus_id": "0000:09:00.0"},
        ]
        with self.assertRaises(ValueError):
            gpu_admin.reconcile_inventory(DETECTED, records)


if __name__ == "__main__":
    unittest.main()
