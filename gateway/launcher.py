#!/usr/bin/env python3

import os

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
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

    def do_POST(self):
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
