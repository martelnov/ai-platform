import hashlib
import hmac
import json
import socket
import os
from email.header import decode_header, make_header
from pathlib import Path
from urllib.parse import parse_qs

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


app = FastAPI(
    title="AI Platform Control Plane",
    version="0.1.0",
)

templates = Jinja2Templates(
    directory="/app/app/templates",
)

SECRET_ALGORITHM = "aes-256-gcm"
SECRET_KEY_VERSION = 1


def read_secret_file(env_name: str) -> str:
    path = os.environ.get(env_name)

    if not path:
        raise RuntimeError(f"{env_name} is not configured")

    return Path(path).read_text().strip()


def master_key() -> bytes:
    path = os.environ.get("MASTER_KEY_FILE")

    if not path:
        raise RuntimeError("MASTER_KEY_FILE is not configured")

    value = Path(path).read_bytes()

    if len(value) != 32:
        raise RuntimeError("master key must be exactly 32 bytes")

    return value


def db_connect():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "ai_platform"),
        user=os.environ.get("DB_USER", "ai_platform"),
        password=read_secret_file("DB_PASSWORD_FILE"),
    )


def decode_header_value(value: str | None) -> str | None:
    if not value:
        return None

    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def tailscale_identity(request: Request):
    login = decode_header_value(
        request.headers.get("tailscale-user-login")
    )
    name = decode_header_value(
        request.headers.get("tailscale-user-name")
    )

    if login:
        login = login.strip().lower()

    return {
        "login": login,
        "name": name,
    }


def csrf_token(login: str, action: str) -> str:
    return hmac.new(
        master_key(),
        f"{action}:{login}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def secret_aad(name: str) -> bytes:
    return (
        f"ai-platform-secret:v{SECRET_KEY_VERSION}:{name}"
    ).encode("utf-8")


def encrypt_secret(name: str, plaintext: str):
    nonce = os.urandom(12)

    ciphertext = AESGCM(master_key()).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        secret_aad(name),
    )

    return nonce, ciphertext


def decrypt_secret(conn, name: str) -> bytes:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                algorithm,
                key_version,
                nonce,
                ciphertext
            FROM platform_secrets
            WHERE name = %s
            """,
            (name,),
        )

        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Secret not configured",
        )

    algorithm, key_version, nonce, ciphertext = row

    if algorithm != SECRET_ALGORITHM:
        raise HTTPException(
            status_code=500,
            detail="Unsupported secret algorithm",
        )

    if key_version != SECRET_KEY_VERSION:
        raise HTTPException(
            status_code=500,
            detail="Unsupported secret key version",
        )

    try:
        return AESGCM(master_key()).decrypt(
            bytes(nonce),
            bytes(ciphertext),
            secret_aad(name),
        )
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Secret decryption failed",
        )


def host_agent_request(payload, timeout=120):
    with socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    ) as sock:
        sock.settimeout(timeout)
        sock.connect("/run/ai-platform/agent.sock")

        sock.sendall(
            json.dumps(
                payload,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

        data = b""

        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)

            if not chunk:
                raise RuntimeError(
                    "host agent closed connection"
                )

            data += chunk

            if len(data) > 65536:
                raise RuntimeError(
                    "host agent response too large"
                )

    return json.loads(data.decode("utf-8"))


def host_agent_ping():
    try:
        with socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        ) as sock:
            sock.settimeout(2)
            sock.connect("/run/ai-platform/agent.sock")
            sock.sendall(b'{"op":"ping"}\n')

            data = b""

            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)

                if not chunk:
                    raise RuntimeError(
                        "host agent closed connection"
                    )

                data += chunk

                if len(data) > 65536:
                    raise RuntimeError(
                        "host agent response too large"
                    )

        response = json.loads(data.decode("utf-8"))

        return {
            "ok": response.get("ok") is True,
            "version": response.get("version"),
            "error": response.get("error"),
        }

    except Exception as exc:
        return {
            "ok": False,
            "version": None,
            "error": str(exc),
        }


def require_admin(request: Request, conn):
    identity = tailscale_identity(request)

    if not identity["login"]:
        raise HTTPException(
            status_code=401,
            detail="Tailscale identity required",
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, enabled
            FROM platform_admins
            WHERE tailscale_login = %s
            """,
            (identity["login"],),
        )

        row = cur.fetchone()

        if not row or not row[1]:
            raise HTTPException(
                status_code=403,
                detail="Administrator access required",
            )

        cur.execute(
            """
            UPDATE platform_admins
            SET last_seen_at = now()
            WHERE id = %s
            """,
            (row[0],),
        )

    return identity


@app.get("/health")
def health():
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        )

    return {
        "status": "ok",
        "database": "ok",
    }


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    identity = tailscale_identity(request)

    if not identity["login"]:
        raise HTTPException(
            status_code=401,
            detail="Tailscale identity required",
        )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM platform_admins"
            )
            admin_count = cur.fetchone()[0]

        if admin_count == 0:
            return templates.TemplateResponse(
                request=request,
                name="bootstrap-admin.html",
                context={
                    "identity": identity,
                    "csrf": csrf_token(
                        identity["login"],
                        "admin-bootstrap",
                    ),
                },
            )

        identity = require_admin(request, conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version
                FROM schema_migrations
                ORDER BY version
                """
            )

            migrations = ",".join(
                str(row[0])
                for row in cur.fetchall()
            )

            cur.execute(
                """
                SELECT
                    name,
                    algorithm,
                    key_version,
                    expires_at,
                    updated_at
                FROM platform_secrets
                ORDER BY name
                """
            )

            secrets = [
                {
                    "name": row[0],
                    "algorithm": row[1],
                    "key_version": row[2],
                    "expires_at": row[3],
                    "updated_at": row[4],
                }
                for row in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT
                    uc.id,
                    u.username,
                    uc.connector_type,
                    uc.node_name,
                    uc.state_path,
                    uc.status,
                    uc.address,
                    uc.last_error,
                    uc.updated_at
                FROM user_connectors uc
                JOIN users u
                  ON u.id = uc.user_id
                ORDER BY u.username, uc.connector_type
                """
            )

            connectors = [
                {
                    "id": row[0],
                    "username": row[1],
                    "connector_type": row[2],
                    "node_name": row[3],
                    "state_path": row[4],
                    "status": row[5],
                    "address": row[6],
                    "last_error": row[7],
                    "updated_at": row[8],
                }
                for row in cur.fetchall()
            ]

            for connector in connectors:
                connector["csrf"] = csrf_token(
                    identity["login"],
                    f"connector-provision:{connector['id']}",
                )

    response = templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "migrations": migrations,
            "secrets": secrets,
            "identity": identity,
            "connectors": connectors,
            "secret_csrf": csrf_token(
                identity["login"],
                "secret-set:tailscale.provisioning",
            ),
            "tailscale_api_csrf": csrf_token(
                identity["login"],
                "secret-set:tailscale.api",
            ),
            "host_agent": host_agent_ping(),
        },
    )

    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/admin/bootstrap")
async def bootstrap_admin(request: Request):
    identity = tailscale_identity(request)

    if not identity["login"]:
        raise HTTPException(
            status_code=401,
            detail="Tailscale identity required",
        )

    form = parse_qs(
        (await request.body()).decode("utf-8")
    )

    supplied = form.get("csrf", [""])[0]

    expected = csrf_token(
        identity["login"],
        "admin-bootstrap",
    )

    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=403,
            detail="Invalid CSRF token",
        )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "LOCK TABLE platform_admins IN EXCLUSIVE MODE"
            )

            cur.execute(
                "SELECT count(*) FROM platform_admins"
            )

            if cur.fetchone()[0] != 0:
                raise HTTPException(
                    status_code=409,
                    detail="Administrator already configured",
                )

            cur.execute(
                """
                INSERT INTO platform_admins (
                    tailscale_login,
                    display_name,
                    last_seen_at
                )
                VALUES (%s, %s, now())
                """,
                (
                    identity["login"],
                    identity["name"],
                ),
            )

    return RedirectResponse(
        url="/admin",
        status_code=303,
    )


@app.post("/admin/secrets/tailscale-provisioning")
async def set_tailscale_secret(request: Request):
    with db_connect() as conn:
        identity = require_admin(request, conn)

        form = parse_qs(
            (await request.body()).decode("utf-8")
        )

        supplied_csrf = form.get("csrf", [""])[0]

        expected_csrf = csrf_token(
            identity["login"],
            "secret-set:tailscale.provisioning",
        )

        if not hmac.compare_digest(
            supplied_csrf,
            expected_csrf,
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid CSRF token",
            )

        value = form.get("value", [""])[0].strip()

        if not value:
            raise HTTPException(
                status_code=400,
                detail="Secret must not be empty",
            )

        if len(value) > 4096:
            raise HTTPException(
                status_code=400,
                detail="Secret is too long",
            )

        nonce, ciphertext = encrypt_secret(
            "tailscale.provisioning",
            value,
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_secrets (
                    name,
                    description,
                    algorithm,
                    key_version,
                    nonce,
                    ciphertext
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (name)
                DO UPDATE SET
                    description = EXCLUDED.description,
                    algorithm = EXCLUDED.algorithm,
                    key_version = EXCLUDED.key_version,
                    nonce = EXCLUDED.nonce,
                    ciphertext = EXCLUDED.ciphertext,
                    updated_at = now()
                """,
                (
                    "tailscale.provisioning",
                    "Tailscale connector provisioning auth key",
                    SECRET_ALGORITHM,
                    SECRET_KEY_VERSION,
                    nonce,
                    ciphertext,
                ),
            )

    return RedirectResponse(
        url="/admin",
        status_code=303,
    )


@app.post("/admin/secrets/tailscale-api")
async def set_tailscale_api_secret(request: Request):
    with db_connect() as conn:
        identity = require_admin(request, conn)

        form = parse_qs(
            (await request.body()).decode("utf-8")
        )

        supplied_csrf = form.get("csrf", [""])[0]

        expected_csrf = csrf_token(
            identity["login"],
            "secret-set:tailscale.api",
        )

        if not hmac.compare_digest(
            supplied_csrf,
            expected_csrf,
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid CSRF token",
            )

        value = form.get("value", [""])[0].strip()

        if not value:
            raise HTTPException(
                status_code=400,
                detail="Secret must not be empty",
            )

        if len(value) > 4096:
            raise HTTPException(
                status_code=400,
                detail="Secret is too long",
            )

        nonce, ciphertext = encrypt_secret(
            "tailscale.api",
            value,
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_secrets (
                    name,
                    description,
                    algorithm,
                    key_version,
                    nonce,
                    ciphertext
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (name)
                DO UPDATE SET
                    description = EXCLUDED.description,
                    algorithm = EXCLUDED.algorithm,
                    key_version = EXCLUDED.key_version,
                    nonce = EXCLUDED.nonce,
                    ciphertext = EXCLUDED.ciphertext,
                    updated_at = now()
                """,
                (
                    "tailscale.api",
                    "Tailscale API access token",
                    SECRET_ALGORITHM,
                    SECRET_KEY_VERSION,
                    nonce,
                    ciphertext,
                ),
            )

    return RedirectResponse(
        url="/admin",
        status_code=303,
    )


@app.get("/admin/secrets/tailscale-provisioning/check")
def check_tailscale_secret(request: Request):
    with db_connect() as conn:
        require_admin(request, conn)

        decrypt_secret(
            conn,
            "tailscale.provisioning",
        )

    return {
        "secret": "tailscale.provisioning",
        "status": "decrypt-ok",
    }


@app.post("/admin/connectors/{connector_id}/provision")
async def provision_connector(
    connector_id: int,
    request: Request,
):
    with db_connect() as conn:
        identity = require_admin(request, conn)

        form = parse_qs(
            (await request.body()).decode("utf-8")
        )

        supplied_csrf = form.get("csrf", [""])[0]

        expected_csrf = csrf_token(
            identity["login"],
            f"connector-provision:{connector_id}",
        )

        if not hmac.compare_digest(
            supplied_csrf,
            expected_csrf,
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid CSRF token",
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    connector_type,
                    node_name,
                    state_path,
                    enabled
                FROM user_connectors
                WHERE id = %s
                """,
                (connector_id,),
            )

            row = cur.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Connector not found",
            )

        connector_type, node_name, state_path, enabled = row

        if not enabled:
            raise HTTPException(
                status_code=409,
                detail="Connector is disabled",
            )

        if connector_type != "tailscale":
            raise HTTPException(
                status_code=400,
                detail="Unsupported connector type",
            )

        auth_key = decrypt_secret(
            conn,
            "tailscale.provisioning",
        ).decode("utf-8")

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_connectors
                SET status = 'provisioning',
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (connector_id,),
            )

        conn.commit()

    try:
        result = host_agent_request(
            {
                "op": "tailscale_connector_provision",
                "connector_id": connector_id,
                "node_name": node_name,
                "state_path": state_path,
                "auth_key": auth_key,
            },
            timeout=120,
        )

        if not result.get("ok"):
            raise RuntimeError(
                result.get("error")
                or "connector provisioning failed"
            )

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_connectors
                    SET status = 'running',
                        address = %s,
                        provider_id = %s,
                        dns_name = %s,
                        last_seen_at = now(),
                        last_error = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        result.get("address"),
                        result.get("provider_id"),
                        result.get("dns_name"),
                        connector_id,
                    ),
                )

    except Exception as exc:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_connectors
                    SET status = 'failed',
                        last_error = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        str(exc)[:2000],
                        connector_id,
                    ),
                )

        raise HTTPException(
            status_code=500,
            detail="Connector provisioning failed",
        )

    return RedirectResponse(
        url="/admin",
        status_code=303,
    )
