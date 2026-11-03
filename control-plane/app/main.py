import hashlib
import hmac
import json
import socket
import os
from email.header import decode_header, make_header
from pathlib import Path
from urllib.parse import parse_qs, quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb

from app.db import db_connect


app = FastAPI(
    title="AI Platform Control Plane",
    version="0.1.0",
)

templates = Jinja2Templates(
    directory="/app/app/templates",
)

SECRET_ALGORITHM = "aes-256-gcm"
SECRET_KEY_VERSION = 1


def format_bytes(value):
    if value is None:
        return "-"

    value = int(value)

    units = (
        ("TiB", 1024 ** 4),
        ("GiB", 1024 ** 3),
        ("MiB", 1024 ** 2),
        ("KiB", 1024),
    )

    for unit, size in units:
        if value >= size:
            number = value / size

            if number.is_integer():
                return f"{int(number)} {unit}"

            return f"{number:.1f} {unit}"

    return f"{value} B"


def master_key() -> bytes:
    path = os.environ.get("MASTER_KEY_FILE")

    if not path:
        raise RuntimeError("MASTER_KEY_FILE is not configured")

    value = Path(path).read_bytes()

    if len(value) != 32:
        raise RuntimeError("master key must be exactly 32 bytes")

    return value


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


def upsert_secret(
    conn,
    name: str,
    plaintext: str,
    description: str | None = None,
    metadata: dict | None = None,
):
    nonce, ciphertext = encrypt_secret(
        name,
        plaintext,
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
                ciphertext,
                metadata
            )
            VALUES (
                %s,
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
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            (
                name,
                description,
                SECRET_ALGORITHM,
                SECRET_KEY_VERSION,
                nonce,
                ciphertext,
                Jsonb(metadata or {}),
            ),
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
                    id,
                    username,
                    display_name,
                    email,
                    linux_uid,
                    linux_gid,
                    enabled,
                    provisioning_status,
                    provisioning_error
                FROM users
                ORDER BY username
                """
            )

            users = [
                {
                    "id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "email": row[3],
                    "linux_uid": row[4],
                    "linux_gid": row[5],
                    "enabled": row[6],
                    "provisioning_status": row[7],
                    "provisioning_error": row[8],
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
            "users": users,
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


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    with db_connect() as conn:
        identity = require_admin(request, conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    username,
                    display_name,
                    email,
                    linux_uid,
                    linux_gid,
                    enabled,
                    provisioning_status,
                    provisioning_error
                FROM users
                ORDER BY username
                """
            )

            users = [
                {
                    "id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "email": row[3],
                    "linux_uid": row[4],
                    "linux_gid": row[5],
                    "enabled": row[6],
                    "provisioning_status": row[7],
                    "provisioning_error": row[8],
                }
                for row in cur.fetchall()
            ]

    response = templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "identity": identity,
            "users": users,
        },
    )

    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(request: Request, user_id: int):
    with db_connect() as conn:
        identity = require_admin(request, conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    username,
                    display_name,
                    email,
                    linux_uid,
                    linux_gid,
                    enabled,
                    provisioning_status,
                    provisioning_error
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            row = cur.fetchone()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail="User not found",
                )

            user = {
                "id": row[0],
                "username": row[1],
                "display_name": row[2],
                "email": row[3],
                "linux_uid": row[4],
                "linux_gid": row[5],
                "enabled": row[6],
                "provisioning_status": row[7],
                "provisioning_error": row[8],
            }

            cur.execute(
                """
                SELECT
                    id,
                    connector_type,
                    node_name,
                    status,
                    address,
                    dns_name,
                    last_error
                FROM user_connectors
                WHERE user_id = %s
                ORDER BY connector_type
                """,
                (user_id,),
            )

            connectors = [
                {
                    "id": row[0],
                    "connector_type": row[1],
                    "node_name": row[2],
                    "status": row[3],
                    "address": row[4],
                    "dns_name": row[5],
                    "last_error": row[6],
                }
                for row in cur.fetchall()
            ]

            for connector in connectors:
                connector["csrf"] = csrf_token(
                    identity["login"],
                    f"connector-provision:{connector['id']}",
                )

            cur.execute(
                """
                SELECT
                    sr.name,
                    sr.backend_type,
                    fq.soft_bytes,
                    fq.hard_bytes,
                    fq.effective_soft_bytes,
                    fq.effective_hard_bytes,
                    fq.last_applied_at,
                    fq.last_checked_at,
                    fq.last_error
                FROM filesystem_quotas fq
                JOIN storage_resources sr
                  ON sr.id = fq.storage_resource_id
                WHERE fq.user_id = %s
                ORDER BY sr.id
                """,
                (user_id,),
            )

            storage = []

            for row in cur.fetchall():
                resource_name = row[0]

                if resource_name == "local-data":
                    label = "DATA"
                elif resource_name == "local-models":
                    label = "MODELS"
                else:
                    label = resource_name

                storage.append({
                    "label": label,
                    "resource": resource_name,
                    "backend_type": row[1],
                    "soft": format_bytes(row[2]),
                    "hard": format_bytes(row[3]),
                    "effective_soft": format_bytes(row[4]),
                    "effective_hard": format_bytes(row[5]),
                    "last_applied_at": row[6],
                    "last_checked_at": row[7],
                    "last_error": row[8],
                })

            cur.execute(
                """
                SELECT
                    id,
                    tree,
                    mount_path,
                    scope,
                    editable_by_user,
                    source_type,
                    source_ref,
                    filesystem_type,
                    encrypted,
                    encryption_type,
                    dedup_mode,
                    enabled
                FROM user_mounts
                WHERE user_id = %s
                ORDER BY tree, mount_path
                """,
                (user_id,),
            )

            mounts = [
                {
                    "id": row[0],
                    "tree": row[1],
                    "mount_path": row[2],
                    "scope": row[3],
                    "editable_by_user": row[4],
                    "source_type": row[5],
                    "source_ref": row[6],
                    "filesystem_type": row[7],
                    "encrypted": row[8],
                    "encryption_type": row[9],
                    "dedup_mode": row[10],
                    "enabled": row[11],
                }
                for row in cur.fetchall()
            ]

            for mount in mounts:
                mount["remove_csrf"] = csrf_token(
                    identity["login"],
                    f"mount-remove:{mount['id']}",
                )

            cur.execute(
                """
                SELECT
                    id,
                    tree,
                    root_path,
                    encryption_type,
                    enabled,
                    created_at
                FROM user_encryption_roots
                WHERE user_id = %s
                ORDER BY tree, root_path
                """,
                (user_id,),
            )

            encryption_roots = [
                {
                    "id": row[0],
                    "tree": row[1],
                    "root_path": row[2],
                    "encryption_type": row[3],
                    "enabled": row[4],
                    "created_at": row[5],
                }
                for row in cur.fetchall()
            ]

        for root in encryption_roots:
            root["state"] = "unknown"
            root["policy"] = None
            root["status_error"] = None

            try:
                result = host_agent_request(
                    {
                        "op": "user_encryption_status",
                        "username": user["username"],
                        "tree": root["tree"],
                        "root_path": root["root_path"],
                    },
                    timeout=10,
                )

                if not result.get("ok"):
                    raise RuntimeError(
                        result.get("error")
                        or "encryption status failed"
                    )

                state = result.get("state")

                if state not in (
                    "not_initialized",
                    "locked",
                    "unlocked",
                ):
                    raise RuntimeError(
                        f"unexpected encryption state: {state}"
                    )

                root["state"] = state
                root["policy"] = result.get("policy")

            except Exception as exc:
                root["status_error"] = str(exc)[:300]

            root["remove_csrf"] = csrf_token(
                identity["login"],
                f"encryptable-remove:{root['id']}",
            )

    response = templates.TemplateResponse(
        request=request,
        name="user-detail.html",
        context={
            "identity": identity,
            "user": user,
            "error": request.query_params.get("error"),
            "encryption_error": request.query_params.get("encryption_error"),
            "storage": storage,
            "encryption_roots": encryption_roots,
            "encryption_create_csrf": csrf_token(
                identity["login"],
                f"encryptable-create:{user_id}",
            ),
            "mounts": mounts,
            "mount_create_csrf": csrf_token(
                identity["login"],
                f"mount-create:{user_id}",
            ),
            "connectors": connectors,
        },
    )

    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/admin/config", response_class=HTMLResponse)
def admin_config(request: Request):
    with db_connect() as conn:
        identity = require_admin(request, conn)

        with conn.cursor() as cur:
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

    response = templates.TemplateResponse(
        request=request,
        name="config.html",
        context={
            "identity": identity,
            "secrets": secrets,
            "secret_csrf": csrf_token(
                identity["login"],
                "secret-set:tailscale.provisioning",
            ),
            "tailscale_api_csrf": csrf_token(
                identity["login"],
                "secret-set:tailscale.api",
            ),
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


@app.post("/admin/users/{user_id}/encryptable-storage")
async def create_encryptable_storage(
    user_id: int,
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
            f"encryptable-create:{user_id}",
        )

        if not hmac.compare_digest(
            supplied_csrf,
            expected_csrf,
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid CSRF token",
            )

        tree = form.get("tree", [""])[0].strip()
        root_path = form.get("root_path", [""])[0].strip()

        if tree not in ("data", "models"):
            message = "Invalid storage tree"

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?encryption_error="
                    + quote(message, safe="")
                    + "#encryptable-storage"
                ),
                status_code=303,
            )

        parts = (
            root_path[1:].split("/")
            if root_path.startswith("/")
            else []
        )

        if (
            not root_path.startswith("/")
            or root_path == "/"
            or any(
                not part
                or part in (".", "..")
                or "\x00" in part
                for part in parts
            )
        ):
            message = (
                "Path must be below the user storage root, "
                "for example /private"
            )

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?encryption_error="
                    + quote(message, safe="")
                    + "#encryptable-storage"
                ),
                status_code=303,
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            if not cur.fetchone():
                raise HTTPException(
                    status_code=404,
                    detail="User not found",
                )

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_encryption_roots (
                        user_id,
                        tree,
                        root_path,
                        encryption_type,
                        enabled
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        'fscrypt',
                        true
                    )
                    """,
                    (
                        user_id,
                        tree,
                        root_path,
                    ),
                )

            conn.commit()

        except Exception:
            conn.rollback()

            message = (
                "Cannot mark this path as encryptable. "
                "It overlaps an existing mount or "
                "encryptable storage path."
            )

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?encryption_error="
                    + quote(message, safe="")
                    + "#encryptable-storage"
                ),
                status_code=303,
            )

    return RedirectResponse(
        url=f"/admin/users/{user_id}#encryptable-storage",
        status_code=303,
    )


@app.post(
    "/admin/users/{user_id}/encryptable-storage/{root_id}/remove"
)
async def remove_encryptable_storage(
    user_id: int,
    root_id: int,
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
            f"encryptable-remove:{root_id}",
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
                    u.username,
                    er.tree,
                    er.root_path,
                    er.encryption_type
                FROM user_encryption_roots er
                JOIN users u
                  ON u.id = er.user_id
                WHERE er.id = %s
                  AND er.user_id = %s
                FOR UPDATE
                """,
                (
                    root_id,
                    user_id,
                ),
            )

            row = cur.fetchone()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail="Encryptable storage not found",
                )

            (
                username,
                tree,
                root_path,
                encryption_type,
            ) = row

        if encryption_type != "fscrypt":
            conn.rollback()

            message = (
                "Unsupported encryption type: "
                + str(encryption_type)
            )

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?encryption_error="
                    + quote(message, safe="")
                    + "#encryptable-storage"
                ),
                status_code=303,
            )

        try:
            result = host_agent_request(
                {
                    "op": "user_encryption_status",
                    "username": username,
                    "tree": tree,
                    "root_path": root_path,
                },
                timeout=10,
            )

            if not result.get("ok"):
                raise RuntimeError(
                    result.get("error")
                    or "encryption status failed"
                )

            state = result.get("state")

            if state != "not_initialized":
                conn.rollback()

                message = (
                    f"Cannot disallow {tree.upper()} {root_path}; "
                    f"encryption state is {state}. "
                    "Encrypted storage must remain configured."
                )

                return RedirectResponse(
                    url=(
                        f"/admin/users/{user_id}?encryption_error="
                        + quote(message, safe="")
                        + "#encryptable-storage"
                    ),
                    status_code=303,
                )

        except Exception as exc:
            conn.rollback()

            message = (
                "Cannot verify encryption state: "
                + str(exc)[:500]
            )

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?encryption_error="
                    + quote(message, safe="")
                    + "#encryptable-storage"
                ),
                status_code=303,
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_encryption_roots
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    root_id,
                    user_id,
                ),
            )

            if cur.rowcount != 1:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Encryption configuration changed",
                )

        conn.commit()

    return RedirectResponse(
        url=f"/admin/users/{user_id}#encryptable-storage",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/mounts/directory")
async def create_platform_directory_mount(
    user_id: int,
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
            f"mount-create:{user_id}",
        )

        if not hmac.compare_digest(
            supplied_csrf,
            expected_csrf,
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid CSRF token",
            )

        tree = form.get("tree", [""])[0].strip()
        mount_path = form.get("mount_path", [""])[0].strip()
        source_ref = form.get("source_ref", [""])[0].strip()

        if tree not in ("data", "models"):
            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?error="
                    + quote("Invalid storage tree", safe="") + "#additional-mounts"
                ),
                status_code=303,
            )

        if not mount_path:
            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?error="
                    + quote("Mount path is required", safe="") + "#additional-mounts"
                ),
                status_code=303,
            )

        if not source_ref:
            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?error="
                    + quote("Source path is required", safe="") + "#additional-mounts"
                ),
                status_code=303,
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT username
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            row = cur.fetchone()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail="User not found",
                )

            username = row[0]

            cur.execute(
                """
                SELECT id
                FROM user_mounts
                WHERE user_id = %s
                  AND tree = %s
                  AND mount_path = %s
                """,
                (
                    user_id,
                    tree,
                    mount_path,
                ),
            )

            if cur.fetchone():
                return RedirectResponse(
                    url=(
                        f"/admin/users/{user_id}?error="
                        + quote(
                            f"Mount path {mount_path} already exists",
                            safe="",
                        )
                    )
                    + "#additional-mounts",
                    status_code=303,
                )

            cur.execute(
                """
                INSERT INTO user_mounts (
                    user_id,
                    tree,
                    mount_path,
                    scope,
                    editable_by_user,
                    source_type,
                    source_ref,
                    encrypted,
                    dedup_mode,
                    enabled
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    'platform',
                    false,
                    'directory',
                    %s,
                    false,
                    'none',
                    true
                )
                """,
                (
                    user_id,
                    tree,
                    mount_path,
                    source_ref,
                ),
            )

        try:
            result = host_agent_request(
                {
                    "op": "user_mount_apply",
                    "username": username,
                    "tree": tree,
                    "mount_path": mount_path,
                    "scope": "platform",
                    "source_type": "directory",
                    "source_ref": source_ref,
                },
                timeout=120,
            )

            if not result.get("ok"):
                raise RuntimeError(
                    result.get("error")
                    or "mount apply failed"
                )

        except Exception as exc:
            conn.rollback()

            message = (
                "Mount apply failed: "
                + str(exc)[:500]
            )

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?error="
                    + quote(message, safe="")
                    + "#additional-mounts"
                ),
                status_code=303,
            )

        conn.commit()

    return RedirectResponse(
        url=f"/admin/users/{user_id}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/mounts/{mount_id}/remove")
async def remove_platform_mount(
    user_id: int,
    mount_id: int,
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
            f"mount-remove:{mount_id}",
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
                    u.username,
                    um.tree,
                    um.mount_path,
                    um.scope,
                    um.source_type,
                    um.source_ref
                FROM user_mounts um
                JOIN users u
                  ON u.id = um.user_id
                WHERE um.id = %s
                  AND um.user_id = %s
                """,
                (
                    mount_id,
                    user_id,
                ),
            )

            row = cur.fetchone()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail="Mount not found",
                )

            (
                username,
                tree,
                mount_path,
                scope,
                source_type,
                source_ref,
            ) = row

            if scope != "platform":
                message = (
                    "Only platform-managed mounts can be "
                    "removed from this interface"
                )

                return RedirectResponse(
                    url=(
                        f"/admin/users/{user_id}?error="
                        + quote(message, safe="")
                        + "#additional-mounts"
                    ),
                    status_code=303,
                )

            cur.execute(
                """
                SELECT id, mount_path
                FROM user_mounts
                WHERE user_id = %s
                  AND tree = %s
                  AND id <> %s
                ORDER BY mount_path
                """,
                (
                    user_id,
                    tree,
                    mount_id,
                ),
            )

            nested_mounts = [
                child_path
                for _, child_path in cur.fetchall()
                if child_path.startswith(
                    mount_path + "/"
                )
            ]

            if nested_mounts:
                message = (
                    f"Cannot remove {mount_path}; "
                    "nested mounts exist: "
                    + ", ".join(nested_mounts)
                )

                return RedirectResponse(
                    url=(
                        f"/admin/users/{user_id}?error="
                        + quote(message, safe="")
                        + "#additional-mounts"
                    ),
                    status_code=303,
                )

        try:
            result = host_agent_request(
                {
                    "op": "user_mount_remove",
                    "username": username,
                    "tree": tree,
                    "mount_path": mount_path,
                    "scope": scope,
                    "source_type": source_type,
                    "source_ref": source_ref,
                },
                timeout=120,
            )

            if not result.get("ok"):
                raise RuntimeError(
                    result.get("error")
                    or "mount remove failed"
                )

        except Exception as exc:
            conn.rollback()

            message = (
                "Mount remove failed: "
                + str(exc)[:500]
            )

            return RedirectResponse(
                url=(
                    f"/admin/users/{user_id}?error="
                    + quote(message, safe="")
                    + "#additional-mounts"
                ),
                status_code=303,
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_mounts
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    mount_id,
                    user_id,
                ),
            )

            if cur.rowcount != 1:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Mount configuration changed",
                )

        conn.commit()

    return RedirectResponse(
        url=f"/admin/users/{user_id}#additional-mounts",
        status_code=303,
    )


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
