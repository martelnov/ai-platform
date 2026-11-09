"""Stdlib-only test of connector-scoped lifecycle routes and DB selection."""
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'control-plane'))

http_stub = types.ModuleType('fastapi')
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
http_stub.HTTPException = HTTPException
http_stub.Request = type('Request', (), {})
http_stub.FastAPI = FastAPI
fake_db = types.ModuleType('app.db')
fake_db.db_connect = lambda: None
with patch.dict(sys.modules, {'fastapi': http_stub, 'app.db': fake_db}):
    from app import gateway_api
    from app import connector_auth


class Request:
    def __init__(self, headers, payload=None):
        self.headers = headers
        self.payload = payload


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = None
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def execute(self, query, params):
        self.params = params
        assert 'w.user_id = %s' in query and 'w.connector_id = %s' in query
    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def cursor(self):
        return self.cursor_obj


class SalamanderGatewayTests(unittest.TestCase):
    def setUp(self):
        self.secret = TemporaryDirectory()
        Path(self.secret.name, 'key').write_bytes(b'K' * 64)
        self.addCleanup(self.secret.cleanup)
        self.patch_key = patch.object(connector_auth, 'KEY_PATH',
                                      str(Path(self.secret.name, 'key')))
        self.patch_key.start()
        self.addCleanup(self.patch_key.stop)

        self.cursor = Cursor([(42,)])
        self.patch_db = patch.object(gateway_api, 'db_connect',
                                     return_value=Connection(self.cursor))
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.patch_identity = patch.object(gateway_api, 'resolve_connector_identity',
            return_value={
                'user': {'id': 7, 'username': 'test'},
                'connector': {'id': 8, 'dns_name': 'ai-test.tail05a1b5.ts.net.'}
            })
        self.patch_identity.start()
        self.addCleanup(self.patch_identity.stop)
        self.agent = patch.object(gateway_api, 'host_agent_request',
                                  return_value={'ok': True, 'status': 'running'})
        self.agent_mock = self.agent.start()
        self.addCleanup(self.agent.stop)

    def request(self, name='node-A', payload=None):
        return Request({
            'x-ai-connector-id': name,
            'x-ai-connector-auth': connector_auth.token_for(name, b'K' * 64),
        }, payload)

    def test_status_fails_for_forged_identity(self):
        request = Request({
            'x-ai-connector-id': 'node-A',
            'x-ai-connector-auth': '0' * 64,
        })
        with self.assertRaises(HTTPException) as ctx:
            gateway_api.gateway_salamander_status(request)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertFalse(self.agent_mock.called)

    def test_status_for_own_workload(self):
        result = gateway_api.gateway_salamander_status(self.request())
        self.assertEqual(result, {
            'registered': True, 'status': 'running',
            'url': 'https://ai-test.tail05a1b5.ts.net:8443/',
        })
        self.assertEqual(self.cursor.params, (7, 8))
        self.agent_mock.assert_called_once_with(
            {'op': 'workload_status', 'workload_id': 42}, timeout=120)

    def test_start_never_trusts_user_supplied_workload_id(self):
        result = gateway_api.gateway_salamander_start(
            self.request(payload={'workload_id': 999}))
        self.assertEqual(result['status'], 'running')
        self.agent_mock.assert_called_once_with(
            {'op': 'workload_start', 'workload_id': 42}, timeout=120)

    def test_stop_for_own_workload(self):
        gateway_api.gateway_salamander_stop(self.request())
        self.agent_mock.assert_called_once_with(
            {'op': 'workload_stop', 'workload_id': 42}, timeout=120)

    def test_missing_workload_cannot_start(self):
        self.cursor.rows = []
        with self.assertRaises(HTTPException) as ctx:
            gateway_api.gateway_salamander_start(self.request())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertFalse(self.agent_mock.called)

    def test_duplicated_workload_is_rejected(self):
        self.cursor.rows = [(42,), (43,)]
        with self.assertRaises(HTTPException) as ctx:
            gateway_api.gateway_salamander_start(self.request())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(self.agent_mock.called)

    def test_bad_dns_never_returned(self):
        self.assertIsNone(gateway_api.salamander_url({
            'connector': {'dns_name': 'https://evil.test/'}}))


if __name__ == '__main__':
    unittest.main()
