#!/usr/bin/env python3

import json
import os
import socketserver
from pathlib import Path


SOCKET_PATH = "/run/ai-platform/agent.sock"
MAX_REQUEST = 65536


class AgentHandler(socketserver.StreamRequestHandler):

    def send(self, data):
        self.wfile.write(
            json.dumps(data, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )

    def handle(self):
        try:
            raw = self.rfile.readline(MAX_REQUEST + 1)

            if len(raw) > MAX_REQUEST:
                self.send({
                    "ok": False,
                    "error": "request too large",
                })
                return

            request = json.loads(raw.decode("utf-8"))
            operation = request.get("op")

            if operation == "ping":
                self.send({
                    "ok": True,
                    "service": "ai-platform-agent",
                    "version": 1,
                })
                return

            self.send({
                "ok": False,
                "error": "unsupported operation",
            })

        except Exception:
            self.send({
                "ok": False,
                "error": "invalid request",
            })


class AgentServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,
):
    daemon_threads = True


def main():
    if os.geteuid() != 0:
        raise SystemExit("ai-platform-agent must run as root")

    path = Path(SOCKET_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        path.unlink()

    try:
        with AgentServer(SOCKET_PATH, AgentHandler) as server:
            os.chmod(SOCKET_PATH, 0o660)
            server.serve_forever()
    finally:
        if path.exists():
            path.unlink()


if __name__ == "__main__":
    main()
