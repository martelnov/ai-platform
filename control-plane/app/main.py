import hashlib
import hmac
import os
from email.header import decode_header, make_header
from pathlib import Path
from urllib.parse import parse_qs

import psycopg
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


def read_secret_file(env_name: str) -> str:
    path = os.environ.get(env_name)

    if not path:
        raise RuntimeError(f"{env_name} is not configured")

    return Path(path).read_text().strip()


def read_binary_secret_file(env_name: str) -> bytes:
    path = os.environ.get(env_name)

    if not path:
        raise RuntimeError(f"{env_name} is not configured")

    value = Path(path).read_bytes()

    if len(value) != 32:
        raise RuntimeError(f"{env_name} must contain exactly 32 bytes")

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


def bootstrap_csrf(login: str) -> str:
    key = read_binary_secret_file("MASTER_KEY_FILE")

    return hmac.new(
        key,
        f"admin-bootstrap:{login}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
                        "csrf": bootstrap_csrf(
                            identity["login"]
                        ),
                    },
                )

            cur.execute(
                """
                SELECT id, enabled
                FROM platform_admins
                WHERE tailscale_login = %s
                """,
                (identity["login"],),
            )

            admin_row = cur.fetchone()

            if not admin_row or not admin_row[1]:
                raise HTTPException(
                    status_code=403,
                    detail="Administrator access required",
                )

            cur.execute(
                """
                UPDATE platform_admins
                SET last_seen_at = now(),
                    updated_at = updated_at
                WHERE id = %s
                """,
                (admin_row[0],),
            )

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

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "migrations": migrations,
            "secrets": secrets,
            "identity": identity,
        },
    )


@app.post("/admin/bootstrap")
async def bootstrap_admin(request: Request):
    identity = tailscale_identity(request)

    if not identity["login"]:
        raise HTTPException(
            status_code=401,
            detail="Tailscale identity required",
        )

    body = await request.body()

    try:
        form = parse_qs(body.decode("utf-8"))
        csrf = form.get("csrf", [""])[0]
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid form data",
        )

    expected = bootstrap_csrf(identity["login"])

    if not hmac.compare_digest(csrf, expected):
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
