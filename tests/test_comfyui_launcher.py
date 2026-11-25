"""ComfyUI launcher proxy security and routing tests."""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "gateway" / "launcher.py"
spec = importlib.util.spec_from_file_location("comfyui_launcher_test", PATH)
launcher = importlib.util.module_from_spec(spec)

with patch.dict(os.environ, {
    "AI_CONNECTOR_PROVIDER_ID": "node-A",
    "AI_CONNECTOR_AUTH_TOKEN": "1" * 64,
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


class ComfyUILauncherTests(unittest.TestCase):
    def handler(self, path, headers=None):
        handler = object.__new__(launcher.Handler)
        handler.path = path
        handler.headers = headers or {}
        handler.respond = Mock()
        return handler

    def test_status_uses_server_side_connector_auth(self):
        handler = self.handler("/api/comfyui")

        with patch.object(
            launcher, "urlopen", return_value=FakeUpstream()
        ) as open_url:
            handler.do_GET()

        req = open_url.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/v1/comfyui"))
        self.assertEqual(req.get_header("X-ai-connector-id"), "node-A")
        self.assertEqual(req.get_header("X-ai-connector-auth"), "1" * 64)
        self.assertEqual(handler.respond.call_args.args[0], 200)

    def test_start_uses_fixed_upstream_path(self):
        handler = self.handler(
            "/api/comfyui/start",
            {"X-AI-Platform-Action": "start"},
        )

        with patch.object(
            launcher, "urlopen", return_value=FakeUpstream()
        ) as open_url:
            handler.do_POST()

        req = open_url.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/v1/comfyui/start"))
        self.assertEqual(handler.respond.call_args.args[0], 200)

    def test_stop_uses_fixed_upstream_path(self):
        handler = self.handler(
            "/api/comfyui/stop",
            {"X-AI-Platform-Action": "stop"},
        )

        with patch.object(
            launcher, "urlopen", return_value=FakeUpstream()
        ) as open_url:
            handler.do_POST()

        req = open_url.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/v1/comfyui/stop"))

    def test_form_post_is_rejected(self):
        handler = self.handler("/api/comfyui/start")

        with patch.object(launcher, "urlopen") as open_url:
            handler.do_POST()

        open_url.assert_not_called()
        self.assertEqual(handler.respond.call_args.args[0], 403)

    def test_wrong_action_header_is_rejected(self):
        handler = self.handler(
            "/api/comfyui/start",
            {"X-AI-Platform-Action": "stop"},
        )

        with patch.object(launcher, "urlopen") as open_url:
            handler.do_POST()

        open_url.assert_not_called()
        self.assertEqual(handler.respond.call_args.args[0], 403)

    def test_unknown_action_is_rejected(self):
        handler = self.handler(
            "/api/comfyui/arbitrary",
            {"X-AI-Platform-Action": "arbitrary"},
        )

        with patch.object(launcher, "urlopen") as open_url:
            handler.do_POST()

        open_url.assert_not_called()
        self.assertEqual(handler.respond.call_args.args[0], 404)


if __name__ == "__main__":
    unittest.main()
