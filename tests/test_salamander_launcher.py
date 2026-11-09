"""Launcher proxies only fixed Salamander actions with server-side identity."""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'gateway' / 'launcher.py'
spec = importlib.util.spec_from_file_location('launcher_under_test', PATH)
launcher = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {
    'AI_CONNECTOR_PROVIDER_ID': 'node-A',
    'AI_CONNECTOR_AUTH_TOKEN': '1' * 64,
}):
    spec.loader.exec_module(launcher)


class FakeUpstream:
    status = 200
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def read(self, n):
        return b'{"registered":true,"status":"running"}'


class LauncherTests(unittest.TestCase):
    def handler(self, path, headers=None):
        handler = object.__new__(launcher.Handler)
        handler.path = path
        handler.headers = headers or {}
        handler.respond = Mock()
        return handler

    def test_form_post_rejected(self):
        handler = self.handler('/api/salamander/start')
        with patch.object(launcher, 'urlopen') as open_url:
            handler.do_POST()
        open_url.assert_not_called()
        self.assertEqual(handler.respond.call_args.args[0], 403)

    def test_post_action_header_is_mandatory(self):
        handler = self.handler('/api/salamander/start',
                               {'X-AI-Platform-Action': 'stop'})
        with patch.object(launcher, 'urlopen') as open_url:
            handler.do_POST()
        open_url.assert_not_called()
        self.assertEqual(handler.respond.call_args.args[0], 403)

    def test_action_is_server_side_authenticated(self):
        handler = self.handler('/api/salamander/start',
                               {'X-AI-Platform-Action': 'start'})
        with patch.object(launcher, 'urlopen', return_value=FakeUpstream()) as open_url:
            handler.do_POST()
        req = open_url.call_args.args[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertTrue(req.full_url.endswith('/v1/salamander/start'))
        self.assertEqual(req.get_header('X-ai-connector-id'), 'node-A')
        self.assertEqual(req.get_header('X-ai-connector-auth'), '1' * 64)
        self.assertEqual(handler.respond.call_args.args[0], 200)

    def test_unknown_post_has_no_backend_access(self):
        handler = self.handler('/api/salamander/arbitrary')
        with patch.object(launcher, 'urlopen') as open_url:
            handler.do_POST()
        open_url.assert_not_called()
        self.assertEqual(handler.respond.call_args.args[0], 404)


if __name__ == '__main__':
    unittest.main()
