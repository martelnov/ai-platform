"""User launcher must allow only reservation routes with trusted identity."""
import importlib.util
import io
import os
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "reservations_launcher_under_test", ROOT / "gateway" / "launcher.py"
)
launcher = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {
    "AI_CONNECTOR_PROVIDER_ID": "connector-A",
    "AI_CONNECTOR_AUTH_TOKEN": "2" * 64,
}):
    spec.loader.exec_module(launcher)


class Upstream:
    def __init__(self, status=200, body=b'{}'):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, n):
        return self.body[:n]


class ReservationLauncherTests(unittest.TestCase):
    def handler(self, path, headers=None, body=b""):
        handler = object.__new__(launcher.Handler)
        handler.path = path
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body)
        handler.respond = Mock()
        handler.log_error = Mock()
        return handler

    def test_my_reservations_get_has_trusted_auth(self):
        handler = self.handler("/api/reservations")
        with patch.object(launcher, "urlopen", return_value=Upstream()) as opener:
            handler.do_GET()
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, launcher.API_URL + "/v1/reservations")
        self.assertEqual(request.get_header("X-ai-connector-id"), "connector-A")
        self.assertEqual(request.get_header("X-ai-connector-auth"), "2" * 64)
        self.assertEqual(handler.respond.call_args.args[0], 200)

    def test_timeline_query_is_whitelisted_and_encoded(self):
        handler = self.handler(
            "/api/reservations/timeline?starts_at=2026-12-01T10%3A00%3A00%2B01%3A00"
            "&ends_at=2026-12-01T11%3A00%3A00%2B01%3A00"
        )
        with patch.object(launcher, "urlopen", return_value=Upstream()) as opener:
            handler.do_GET()
        url = urlsplit(opener.call_args.args[0].full_url)
        self.assertEqual(url.path, "/v1/reservations/timeline")
        self.assertEqual(parse_qs(url.query), {
            "starts_at": ["2026-12-01T10:00:00+01:00"],
            "ends_at": ["2026-12-01T11:00:00+01:00"],
        })

    def test_unknown_or_injected_query_cannot_reach_gateway(self):
        for path in (
            "/api/reservations/arbitrary",
            "/api/reservations/availability?foo=bar",
            "/api/reservations/availability?starts_at=x&starts_at=y&ends_at=z",
            "/api/reservations/timeline?starts_at=x&ends_at=y&user_id=5",
            "/api/reservations?user_id=5",
        ):
            with self.subTest(path=path):
                handler = self.handler(path)
                with patch.object(launcher, "urlopen") as opener:
                    handler.do_GET()
                opener.assert_not_called()
                self.assertIn(handler.respond.call_args.args[0], (400, 404))

    def test_create_forwards_json_not_client_identity(self):
        body = b'{"starts_at":"2026-12-01T10:00:00Z","ends_at":"2026-12-01T11:00:00Z","gpu_count":1}'
        handler = self.handler("/api/reservations", {
            "X-AI-Platform-Action": "reservation-create",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-AI-Connector-ID": "untrusted-client-ID",
        }, body)
        with patch.object(launcher, "urlopen", return_value=Upstream(201)) as opener:
            handler.do_POST()
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, launcher.API_URL + "/v1/reservations")
        self.assertEqual(request.data, body)
        self.assertEqual(request.get_header("X-ai-connector-id"), "connector-A")
        self.assertEqual(handler.respond.call_args.args[0], 201)

    def test_reject_form_post_missing_action_oversized_and_chunked(self):
        for headers, body, expected in (
            ({"Content-Type": "application/json", "Content-Length": "2"}, b"{}", 403),
            ({"X-AI-Platform-Action": "reservation-create", "Content-Type": "text/plain", "Content-Length": "2"}, b"{}", 415),
            ({"X-AI-Platform-Action": "reservation-create", "Content-Type": "application/json", "Content-Length": "4097"}, b"", 413),
            ({"X-AI-Platform-Action": "reservation-create", "Content-Type": "application/json", "Content-Length": "2", "Transfer-Encoding": "chunked"}, b"{}", 403),
        ):
            with self.subTest(headers=headers):
                handler = self.handler("/api/reservations", headers, body)
                with patch.object(launcher, "urlopen") as opener:
                    handler.do_POST()
                opener.assert_not_called()
                self.assertEqual(handler.respond.call_args.args[0], expected)

    def test_cancel_only_numeric_id_with_custom_header(self):
        handler = self.handler("/api/reservations/123", {
            "X-AI-Platform-Action": "reservation-cancel",
        })
        with patch.object(launcher, "urlopen", return_value=Upstream()) as opener:
            handler.do_DELETE()
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.full_url, launcher.API_URL + "/v1/reservations/123")
        self.assertEqual(handler.respond.call_args.args[0], 200)

    def test_cancel_rejects_invalid_target_and_header(self):
        for path, headers in (
            ("/api/reservations/123", {}),
            ("/api/reservations/-123", {"X-AI-Platform-Action": "reservation-cancel"}),
            ("/api/reservations/123/other", {"X-AI-Platform-Action": "reservation-cancel"}),
            ("/api/reservations/123?override=x", {"X-AI-Platform-Action": "reservation-cancel"}),
        ):
            with self.subTest(path=path):
                handler = self.handler(path, headers)
                with patch.object(launcher, "urlopen") as opener:
                    handler.do_DELETE()
                opener.assert_not_called()
                self.assertIn(handler.respond.call_args.args[0], (403, 404))

    def test_gateway_conflict_is_passed_to_browser(self):
        handler = self.handler("/api/reservations")
        error = HTTPError("http://gateway/api", 409, "Conflict", {}, io.BytesIO(b'{"detail":"busy"}'))
        with patch.object(launcher, "urlopen", side_effect=error):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0], 409)
        self.assertEqual(handler.respond.call_args.args[1], b'{"detail":"busy"}')


if __name__ == "__main__":
    unittest.main()
