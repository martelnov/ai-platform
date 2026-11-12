#!/usr/bin/env python3

import os
import re

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen


API_URL = os.environ.get(
    "AI_GATEWAY_API_URL",
    "http://gateway-api:8080"
).rstrip("/")

PROVIDER_ID = os.environ.get("AI_CONNECTOR_PROVIDER_ID", "")
AUTH_TOKEN = os.environ.get("AI_CONNECTOR_AUTH_TOKEN", "")

INDEX = Path(__file__).with_name("index.html")


if not PROVIDER_ID:
    raise SystemExit("AI_CONNECTOR_PROVIDER_ID is required")
if not AUTH_TOKEN:
    raise SystemExit("AI_CONNECTOR_AUTH_TOKEN is required")


class Handler(BaseHTTPRequestHandler):

    def respond(self, status, body, content_type):
        self.send_response(status)

        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'none'"
        )

        self.end_headers()
        self.wfile.write(body)

    def salamander_gateway_request(self, suffix="", method="GET"):
        request = Request(
            API_URL + "/v1/salamander" + suffix,
            headers={
                "X-AI-Connector-ID": PROVIDER_ID,
                "X-AI-Connector-Auth": AUTH_TOKEN,
                "Accept": "application/json",
            },
            method=method,
            data=b"" if method == "POST" else None,
        )
        try:
            with urlopen(request, timeout=125) as upstream:
                status = upstream.status
                result = upstream.read(65537)
        except HTTPError as exc:
            status = exc.code
            result = exc.read(65537)
        except (URLError, TimeoutError, OSError) as exc:
            self.log_error("Salamander Gateway unavailable: %s", exc)
            return self.respond(503, b'{"error":"Gateway unavailable"}',
                                "application/json")
        if len(result) > 65536:
            return self.respond(503, b'{"error":"Gateway response too large"}',
                                "application/json")
        return self.respond(status, result, "application/json")

    def reservation_gateway_request(self, suffix="", method="GET", *,
                                    query="", body=None):
        """Proxy an allowlisted reservation operation as this connector."""
        headers = {
            "X-AI-Connector-ID": PROVIDER_ID,
            "X-AI-Connector-Auth": AUTH_TOKEN,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            API_URL + "/v1/reservations" + suffix + query,
            headers=headers,
            method=method,
            data=body,
        )
        try:
            with urlopen(request, timeout=15) as upstream:
                status = upstream.status
                result = upstream.read(65537)
        except HTTPError as exc:
            status = exc.code
            result = exc.read(65537)
        except (URLError, TimeoutError, OSError) as exc:
            self.log_error("Reservation Gateway unavailable: %s", exc)
            return self.respond(503, b'{"error":"Gateway unavailable"}',
                                "application/json")
        if len(result) > 65536:
            return self.respond(503, b'{"error":"Gateway response too large"}',
                                "application/json")
        return self.respond(status, result, "application/json")

    def do_DELETE(self):
        # Never forward browser-controlled paths or arbitrary reservation IDs.
        match = re.fullmatch(r"/api/reservations/([1-9][0-9]{0,18})", self.path)
        if not match:
            return self.respond(404, b"not found\n", "text/plain; charset=utf-8")
        if (self.headers.get("X-AI-Platform-Action") != "reservation-cancel"
                or self.headers.get("Content-Length", "0") != "0"
                or self.headers.get("Transfer-Encoding") is not None):
            return self.respond(403, b'{"error":"Invalid request"}',
                                "application/json")
        return self.reservation_gateway_request(
            "/" + match.group(1), method="DELETE"
        )

    def do_POST(self):
        if self.path == "/api/reservations":
            # The custom header requires a cross-origin preflight in browsers.
            if (self.headers.get("X-AI-Platform-Action") != "reservation-create"
                    or self.headers.get("Transfer-Encoding") is not None):
                return self.respond(403, b'{"error":"Invalid request"}',
                                    "application/json")
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                return self.respond(415, b'{"error":"JSON required"}',
                                    "application/json")
            try:
                raw_size = self.headers.get("Content-Length", "")
                if not re.fullmatch(r"[0-9]+", raw_size):
                    raise ValueError("Invalid Content-Length")
                size = int(raw_size)
            except ValueError:
                return self.respond(400, b'{"error":"Invalid body length"}',
                                    "application/json")
            if size < 1 or size > 4096:
                return self.respond(413, b'{"error":"Invalid body size"}',
                                    "application/json")
            body = self.rfile.read(size)
            if len(body) != size:
                return self.respond(400, b'{"error":"Incomplete request body"}',
                                    "application/json")
            return self.reservation_gateway_request(method="POST", body=body)

        path = self.path.split("?", 1)[0]
        action = {
            "/api/salamander/start": "start",
            "/api/salamander/stop": "stop",
        }.get(path)
        if not action:
            return self.respond(404, b"not found\n", "text/plain; charset=utf-8")
        # Custom header prevents ordinary cross-site HTML form POSTs.
        if (self.headers.get("X-AI-Platform-Action") != action
                or self.headers.get("Content-Length", "0") != "0"):
            return self.respond(403, b'{"error":"Invalid request"}',
                                "application/json")
        return self.salamander_gateway_request("/" + action, method="POST")

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/health":
            return self.respond(
                200,
                b"ok\n",
                "text/plain; charset=utf-8"
            )

        if path == "/":
            return self.respond(
                200,
                INDEX.read_bytes(),
                "text/html; charset=utf-8"
            )

        if self.path == "/api/reservations":
            return self.reservation_gateway_request()
        if path in ("/api/reservations/availability",
                    "/api/reservations/timeline"):
            # Only starts_at and ends_at, exactly once each, are accepted.
            parts = self.path.split("?", 1)
            if len(parts) != 2 or len(parts[1]) > 512:
                return self.respond(400, b'{"error":"Invalid interval"}',
                                    "application/json")
            try:
                params = parse_qs(parts[1], strict_parsing=True,
                                  max_num_fields=2)
            except ValueError:
                params = {}
            if (set(params) != {"starts_at", "ends_at"}
                    or any(len(values) != 1 or len(values[0]) > 64
                           for values in params.values())):
                return self.respond(400, b'{"error":"Invalid interval"}',
                                    "application/json")
            suffix = path.removeprefix("/api/reservations")
            query = "?" + urlencode({
                "starts_at": params["starts_at"][0],
                "ends_at": params["ends_at"][0],
            })
            return self.reservation_gateway_request(suffix, query=query)

        if path == "/api/salamander":
            return self.salamander_gateway_request()

        if path == "/api/me":
            request = Request(
                API_URL + "/v1/me",
                headers={
                    "X-AI-Connector-ID": PROVIDER_ID,
                    "X-AI-Connector-Auth": AUTH_TOKEN,
                    "Accept": "application/json",
                },
            )

            try:
                with urlopen(request, timeout=5) as upstream:
                    result = upstream.read(65537)

                if len(result) > 65536:
                    raise ValueError("Response too large")

            except (
                HTTPError,
                URLError,
                TimeoutError,
                ValueError,
            ) as exc:
                self.log_error(
                    "Identity API failed: %s",
                    exc
                )

                return self.respond(
                    503,
                    b'{"error":"identity unavailable"}',
                    "application/json"
                )

            return self.respond(
                200,
                result,
                "application/json"
            )

        self.respond(
            404,
            b"not found\n",
            "text/plain; charset=utf-8"
        )


def main():
    print(
        "AI launcher listening on 127.0.0.1:8080",
        flush=True
    )

    ThreadingHTTPServer(
        ("127.0.0.1", 8080),
        Handler
    ).serve_forever()


if __name__ == "__main__":
    main()
