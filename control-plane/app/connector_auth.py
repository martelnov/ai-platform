"""Authenticate per-connector launcher requests to privileged Gateway routes.

The host launcher manager injects a derived connector token. Gateway holds only
its own copy of the root secret; individual launchers never receive that key.
"""

import hashlib
import hmac
import os
from pathlib import Path

from fastapi import HTTPException, Request


KEY_PATH = os.environ.get(
    "CONNECTOR_AUTH_KEY_FILE", "/run/secrets/launcher_auth_key"
)


def token_for(provider_id: str, key: bytes) -> str:
    if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 256:
        raise ValueError("invalid connector identity")
    if len(key) < 32:
        raise ValueError("connector authentication key is too short")
    return hmac.new(
        key,
        b"ai-platform-connector-v1\0" + provider_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def require_connector_auth(request: Request) -> None:
    provider_id = request.headers.get("x-ai-connector-id", "")
    token = request.headers.get("x-ai-connector-auth", "")
    if not provider_id or len(token) != 64:
        raise HTTPException(status_code=403, detail="Connector not authenticated")
    try:
        key = Path(KEY_PATH).read_bytes().strip()
        expected = token_for(provider_id, key)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="Connector authentication unavailable"
        ) from exc
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Connector not authenticated")
