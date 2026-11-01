import os
from pathlib import Path

import psycopg


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
