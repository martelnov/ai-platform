import json
import socket

from fastapi import FastAPI, HTTPException, Request

from app.db import db_connect


app = FastAPI(
    title="AI Platform Gateway API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "gateway-api",
    }


def resolve_connector_identity(conn, request: Request):
    provider_id = request.headers.get("x-ai-connector-id")

    if not provider_id:
        raise HTTPException(
            status_code=401,
            detail="Connector identity required",
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                uc.id,
                uc.provider_id,
                uc.node_name,
                uc.status,
                uc.address,
                uc.dns_name,
                u.id,
                u.username
            FROM user_connectors uc
            JOIN users u
              ON u.id = uc.user_id
            WHERE uc.provider_id = %s
              AND uc.enabled = true
            """,
            (provider_id,),
        )

        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=403,
            detail="Unknown connector",
        )

    return {
        "connector": {
            "id": row[0],
            "provider_id": row[1],
            "node_name": row[2],
            "status": row[3],
            "address": row[4],
            "dns_name": row[5],
        },
        "user": {
            "id": row[6],
            "username": row[7],
        },
    }


@app.get("/v1/storage")
def gateway_storage(request: Request):
    with db_connect() as conn:
        identity = resolve_connector_identity(
            conn,
            request,
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    tree,
                    root_path,
                    encryption_type
                FROM user_encryption_roots
                WHERE user_id = %s
                  AND enabled = true
                ORDER BY tree, root_path
                """,
                (identity["user"]["id"],),
            )

            roots = [
                {
                    "id": row[0],
                    "tree": row[1],
                    "root_path": row[2],
                    "encryption_type": row[3],
                }
                for row in cur.fetchall()
            ]

    for root in roots:
        root["state"] = "unknown"

        try:
            result = host_agent_request(
                {
                    "op": "user_encryption_status",
                    "username": identity["user"]["username"],
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

        except Exception:
            root["state"] = "unknown"

    return {
        "user": identity["user"],
        "storage": roots,
    }


@app.get("/v1/me")
def gateway_me(request: Request):
    provider_id = request.headers.get("x-ai-connector-id")

    if not provider_id:
        raise HTTPException(
            status_code=401,
            detail="Connector identity required",
        )

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    uc.id,
                    uc.provider_id,
                    uc.node_name,
                    uc.status,
                    uc.address,
                    uc.dns_name,
                    u.id,
                    u.username
                FROM user_connectors uc
                JOIN users u
                  ON u.id = uc.user_id
                WHERE uc.provider_id = %s
                  AND uc.enabled = true
                """,
                (provider_id,),
            )

            row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=403,
            detail="Unknown connector",
        )

    return {
        "connector": {
            "id": row[0],
            "provider_id": row[1],
            "node_name": row[2],
            "status": row[3],
            "address": row[4],
            "dns_name": row[5],
        },
        "user": {
            "id": row[6],
            "username": row[7],
        },
    }
