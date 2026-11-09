"""Stdlib-only test of connector HMAC and fail-closed authentication."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / 'control-plane' / 'app' / 'connector_auth.py'
MANAGER = ROOT / 'host' / 'ai-platform-launcher-manager.py'

# The host should not need FastAPI installed merely to run unit tests.
http_stub = types.ModuleType('fastapi')
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
http_stub.HTTPException = HTTPException
http_stub.Request = type('Request', (), {})
with patch.dict(sys.modules, {'fastapi': http_stub}):
    spec = importlib.util.spec_from_file_location('connector_auth', AUTH)
    auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth)

spec2 = importlib.util.spec_from_file_location('launcher_manager', MANAGER)
manager = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(manager)


class ConnectorAuthTests(unittest.TestCase):
    def test_manager_gateway_derive_same_token(self):
        with patch.object(manager.Path, 'read_bytes', return_value=b'0' * 64):
            self.assertEqual(manager.connector_token('nodeA'),
                             auth.token_for('nodeA', b'0' * 64))

    def test_distinct_nodes_have_distinct_credentials(self):
        self.assertNotEqual(auth.token_for('nodeA', b'0' * 64),
                            auth.token_for('nodeB', b'0' * 64))

    def test_invalid_token_refused(self):
        request = SimpleNamespace(headers={
            'x-ai-connector-id': 'nodeA',
            'x-ai-connector-auth': 'a' * 64,
        })
        with patch.object(auth.Path, 'read_bytes', return_value=b'0' * 64):
            with self.assertRaises(HTTPException) as ctx:
                auth.require_connector_auth(request)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_valid_token_accepted(self):
        request = SimpleNamespace(headers={
            'x-ai-connector-id': 'nodeA',
            'x-ai-connector-auth': auth.token_for('nodeA', b'0' * 64),
        })
        with patch.object(auth.Path, 'read_bytes', return_value=b'0' * 64):
            auth.require_connector_auth(request)

    def test_missing_secret_refused(self):
        request = SimpleNamespace(headers={
            'x-ai-connector-id': 'nodeA',
            'x-ai-connector-auth': 'a' * 64,
        })
        with patch.object(auth.Path, 'read_bytes', side_effect=FileNotFoundError):
            with self.assertRaises(HTTPException) as ctx:
                auth.require_connector_auth(request)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
