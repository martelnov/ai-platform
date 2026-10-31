import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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


def db_connect():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "ai_platform"),
        user=os.environ.get("DB_USER", "ai_platform"),
        password=read_secret_file("DB_PASSWORD_FILE"),
    )


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
    with db_connect() as conn:
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

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "migrations": migrations,
            "secrets": secrets,
        },
    )
