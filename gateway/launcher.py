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

INDEX = Path(__file__).with_name("index.html")


if not PROVIDER_ID:
    raise SystemExit("AI_CONNECTOR_PROVIDER_ID is required")


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

        if path == "/api/me":
            request = Request(
                API_URL + "/v1/me",
                headers={
                    "X-AI-Connector-ID": PROVIDER_ID,
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
