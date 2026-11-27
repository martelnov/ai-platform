"""ComfyUI CPU workload creation tests."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control-plane"))

http_stub = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class FastAPI:
    def __init__(self, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda f: f

    def post(self, *args, **kwargs):
        return lambda f: f

    def include_router(self, router):
        pass


http_stub.HTTPException = HTTPException
http_stub.Request = type("Request", (), {})
http_stub.FastAPI = FastAPI

fake_db = types.ModuleType("app.db")
fake_db.db_connect = lambda: None

fake_reservations = types.ModuleType("app.reservations")
fake_reservations.make_reservations_router = lambda resolve_identity: None

with patch.dict(sys.modules, {
    "fastapi": http_stub,
    "app.db": fake_db,
    "app.reservations": fake_reservations,
}):
    from app import gateway_api


IDENTITY = {
    "user": {"id": 7, "username": "test"},
    "connector": {"id": 8},
}


class Cursor:
    def __init__(self, hosts=None, profile=(3,), existing=None):
        self.hosts = hosts if hosts is not None else [(1,)]
        self.profile = profile
        self.existing = existing
        self.last_query = ""
        self.inserted = False
        self.insert_params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.last_query = query

        if "INSERT INTO workloads" in query:
            self.inserted = True
            self.insert_params = params

    def fetchall(self):
        if "FROM hosts" in self.last_query:
            return self.hosts

        if "FROM workloads" in self.last_query:
            return self.existing or []

        return []

    def fetchone(self):
        if "FROM application_profiles" in self.last_query:
            return self.profile

        if "FROM workloads" in self.last_query:
            rows = self.existing or []
            return rows[0] if rows else None

        if "INSERT INTO workloads" in self.last_query:
            return (99,)

        return None


class Connection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


class ComfyUIWorkloadTests(unittest.TestCase):

    def test_first_start_creates_cpu_prep_workload(self):
        cursor = Cursor()
        conn = Connection(cursor)

        workload_id = gateway_api.ensure_comfyui_cpu_workload(
            conn, IDENTITY
        )

        self.assertEqual(workload_id, 99)
        self.assertTrue(cursor.inserted)
        self.assertEqual(
            cursor.insert_params,
            (7, 1, 3, 8),
        )

    def test_existing_workload_is_reused(self):
        cursor = Cursor(existing=[(42, 3, 1, 8)])
        conn = Connection(cursor)

        workload_id = gateway_api.ensure_comfyui_cpu_workload(
            conn, IDENTITY
        )

        self.assertEqual(workload_id, 42)
        self.assertFalse(cursor.inserted)

    def test_different_profile_does_not_create_second_workload(self):
        cursor = Cursor(existing=[(42, 4, 1, 8)])
        with self.assertRaises(HTTPException) as ctx:
            gateway_api.ensure_comfyui_cpu_workload(Connection(cursor), IDENTITY)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(cursor.inserted)

    def test_other_connector_cannot_take_existing_workload(self):
        cursor = Cursor(existing=[(42, 3, 1, 9)])
        with self.assertRaises(HTTPException) as ctx:
            gateway_api.ensure_comfyui_cpu_workload(Connection(cursor), IDENTITY)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(cursor.inserted)

    def test_duplicate_workloads_are_rejected(self):
        cursor = Cursor(existing=[(42, 3, 1, 8), (43, 4, 1, 8)])
        with self.assertRaises(HTTPException) as ctx:
            gateway_api.ensure_comfyui_cpu_workload(Connection(cursor), IDENTITY)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(cursor.inserted)

    def test_comfyui_url_uses_connector_dns(self):
        self.assertEqual(gateway_api.comfyui_url({"connector": {"dns_name": "ai-test.example.ts.net"}}), "https://ai-test.example.ts.net:8444/")
        self.assertIsNone(gateway_api.comfyui_url({"connector": {"dns_name": "evil.example.com"}}))

    def test_multiple_hosts_are_rejected(self):
        cursor = Cursor(hosts=[(1,), (2,)])
        conn = Connection(cursor)

        with self.assertRaises(HTTPException) as ctx:
            gateway_api.ensure_comfyui_cpu_workload(conn, IDENTITY)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertFalse(cursor.inserted)

    def test_disabled_profile_is_rejected(self):
        cursor = Cursor(profile=None)
        conn = Connection(cursor)

        with self.assertRaises(HTTPException) as ctx:
            gateway_api.ensure_comfyui_cpu_workload(conn, IDENTITY)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(cursor.inserted)


if __name__ == "__main__":
    unittest.main()


class ComfyUIEndpointTests(unittest.TestCase):
    def setUp(self):
        self.request = type("Request", (), {"headers": {}})()
        self.identity = IDENTITY
        self.conn = Connection(Cursor())

    @patch.object(gateway_api, "salamander_agent_call")
    @patch.object(gateway_api, "comfyui_workload_for")
    def test_status_returns_running(self, workload_for, agent):
        workload_for.return_value = (self.identity, 42)
        agent.return_value = {"ok": True, "status": "running", "ready": True}

        result = gateway_api.gateway_comfyui_status(self.request)
        self.assertIs(result["ready"], True)

        self.assertEqual(result["status"], "running")
        agent.assert_called_once_with("workload_status", 42)

    @patch.object(gateway_api, "salamander_agent_call")
    @patch.object(gateway_api, "ensure_comfyui_cpu_workload", return_value=42)
    @patch.object(gateway_api, "resolve_connector_identity", return_value=IDENTITY)
    @patch.object(gateway_api, "require_connector_auth")
    @patch.object(gateway_api, "db_connect")
    def test_start_creates_and_starts_workload(
        self, db, auth, resolve, ensure, agent
    ):
        db.return_value = self.conn
        agent.return_value = {"ok": True, "status": "running"}

        result = gateway_api.gateway_comfyui_start(self.request)
        self.assertIs(result["ready"], False)

        self.assertEqual(result["status"], "running")
        self.assertTrue(self.conn.committed)
        ensure.assert_called_once_with(self.conn, IDENTITY)
        agent.assert_called_once_with("workload_start", 42)

    @patch.object(gateway_api, "salamander_agent_call")
    @patch.object(gateway_api, "comfyui_workload_for")
    def test_stop_stops_own_workload(self, workload_for, agent):
        workload_for.return_value = (self.identity, 42)
        agent.return_value = {"ok": True, "status": "stopped"}

        result = gateway_api.gateway_comfyui_stop(self.request)

        self.assertEqual(result["status"], "stopped")
        agent.assert_called_once_with("workload_stop", 42)

    @patch.object(gateway_api, "require_connector_auth")
    @patch.object(gateway_api, "ensure_comfyui_cpu_workload")
    def test_start_rejects_unauthorized_connector(self, ensure, auth):
        auth.side_effect = HTTPException(403, "Forbidden")

        with self.assertRaises(HTTPException) as ctx:
            gateway_api.gateway_comfyui_start(self.request)

        self.assertEqual(ctx.exception.status_code, 403)
        ensure.assert_not_called()
