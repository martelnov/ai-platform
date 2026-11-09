import json
import socket

from fastapi import FastAPI, HTTPException, Request

from app.db import db_connect
from app.connector_auth import require_connector_auth


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
    require_connector_auth(request)
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
              AND u.enabled = true
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


def resolve_encryption_root(
    conn,
    user_id: int,
    root_id: int,
    *,
    for_update: bool = False,
):
    sql = """
        SELECT
            id,
            tree,
            root_path,
            encryption_type,
            enabled
        FROM user_encryption_roots
        WHERE id = %s
          AND user_id = %s
    """

    if for_update:
        sql += " FOR UPDATE"

    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                root_id,
                user_id,
            ),
        )

        row = cur.fetchone()

    if not row or not row[4]:
        raise HTTPException(
            status_code=404,
            detail="Storage encryption configuration not found",
        )

    return {
        "id": row[0],
        "tree": row[1],
        "root_path": row[2],
        "encryption_type": row[3],
        "enabled": row[4],
    }


async def read_passphrase(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON request",
        )

    passphrase = body.get("passphrase")

    if not isinstance(passphrase, str) or not passphrase:
        raise HTTPException(
            status_code=400,
            detail="Passphrase is required",
        )

    return passphrase


async def read_passphrase_change(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON request",
        )

    old_passphrase = body.get("old_passphrase")
    new_passphrase = body.get("new_passphrase")

    if not isinstance(old_passphrase, str) or not old_passphrase:
        raise HTTPException(
            status_code=400,
            detail="Old passphrase is required",
        )

    if not isinstance(new_passphrase, str) or not new_passphrase:
        raise HTTPException(
            status_code=400,
            detail="New passphrase is required",
        )

    return old_passphrase, new_passphrase


def fscrypt_agent_request(
    identity,
    root,
    operation,
    *,
    passphrase=None,
    old_passphrase=None,
    new_passphrase=None,
):
    if root["encryption_type"] != "fscrypt":
        raise HTTPException(
            status_code=409,
            detail="Unsupported encryption type",
        )

    payload = {
        "op": operation,
        "username": identity["user"]["username"],
        "tree": root["tree"],
        "root_path": root["root_path"],
    }

    if passphrase is not None:
        payload["passphrase"] = passphrase

    if old_passphrase is not None:
        payload["old_passphrase"] = old_passphrase

    if new_passphrase is not None:
        payload["new_passphrase"] = new_passphrase

    try:
        result = host_agent_request(
            payload,
            timeout=120,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Host encryption service unavailable",
        ) from exc

    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=(
                result.get("error")
                or "Encryption operation failed"
            ),
        )

    return result


@app.post("/v1/storage/{root_id}/enable")
async def gateway_storage_enable(
    root_id: int,
    request: Request,
):
    passphrase = await read_passphrase(request)

    with db_connect() as conn:
        identity = resolve_connector_identity(
            conn,
            request,
        )

        root = resolve_encryption_root(
            conn,
            identity["user"]["id"],
            root_id,
            for_update=True,
        )

        result = fscrypt_agent_request(
            identity,
            root,
            "user_encryption_enable",
            passphrase=passphrase,
        )

        conn.commit()

    return result


@app.post("/v1/storage/{root_id}/lock")
def gateway_storage_lock(
    root_id: int,
    request: Request,
):
    with db_connect() as conn:
        identity = resolve_connector_identity(
            conn,
            request,
        )

        root = resolve_encryption_root(
            conn,
            identity["user"]["id"],
            root_id,
            for_update=True,
        )

        result = fscrypt_agent_request(
            identity,
            root,
            "user_encryption_lock",
        )

        conn.commit()

    return result


@app.post("/v1/storage/{root_id}/change-passphrase")
async def gateway_storage_change_passphrase(
    root_id: int,
    request: Request,
):
    old_passphrase, new_passphrase = (
        await read_passphrase_change(request)
    )

    with db_connect() as conn:
        identity = resolve_connector_identity(
            conn,
            request,
        )

        root = resolve_encryption_root(
            conn,
            identity["user"]["id"],
            root_id,
            for_update=True,
        )

        result = fscrypt_agent_request(
            identity,
            root,
            "user_encryption_change_passphrase",
            old_passphrase=old_passphrase,
            new_passphrase=new_passphrase,
        )

        conn.commit()

    return result


@app.post("/v1/storage/{root_id}/unlock")
async def gateway_storage_unlock(
    root_id: int,
    request: Request,
):
    passphrase = await read_passphrase(request)

    with db_connect() as conn:
        identity = resolve_connector_identity(
            conn,
            request,
        )

        root = resolve_encryption_root(
            conn,
            identity["user"]["id"],
            root_id,
            for_update=True,
        )

        result = fscrypt_agent_request(
            identity,
            root,
            "user_encryption_unlock",
            passphrase=passphrase,
        )

        conn.commit()

    return result


@app.get("/v1/me")
def gateway_me(request: Request):
    with db_connect() as conn:
        return resolve_connector_identity(conn, request)


# User file manager: the browser never supplies a workload/user ID. The
# connector identity is authenticated, then the associated workload is
# resolved exclusively from PostgreSQL.
def salamander_workload_for(request: Request):
    require_connector_auth(request)
    with db_connect() as conn:
        identity = resolve_connector_identity(conn, request)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id
                FROM workloads w
                JOIN application_profiles p
                  ON p.id = w.application_profile_id
                JOIN applications a
                  ON a.id = p.application_id
                WHERE w.user_id = %s
                  AND w.connector_id = %s
                  AND w.mode = 'utility'
                  AND a.code = 'salamander'
                  AND a.enabled = true
                  AND p.enabled = true
                """,
                (identity["user"]["id"], identity["connector"]["id"]),
            )
            rows = cur.fetchall()
    if len(rows) > 1:
        raise HTTPException(status_code=409,
                            detail="Multiple Salamander workloads configured")
    return identity, (rows[0][0] if rows else None)


def salamander_url(identity):
    # DNS name comes from the database (Tailscale connector), never the browser.
    import re
    name = (identity["connector"].get("dns_name") or "").lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net", name):
        return None
    return f"https://{name}:8443/"


def salamander_agent_call(operation, workload_id):
    try:
        result = host_agent_request(
            {"op": operation, "workload_id": workload_id},
            timeout=120,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503,
                            detail="Host workload service unavailable") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error", "Workload operation failed") if isinstance(result, dict) else "Workload operation failed"
        raise HTTPException(status_code=409, detail=str(error)[:250])
    return result


@app.get("/v1/salamander")
def gateway_salamander_status(request: Request):
    identity, workload_id = salamander_workload_for(request)
    if workload_id is None:
        return {"registered": False, "status": "unavailable", "url": None}
    result = salamander_agent_call("workload_status", workload_id)
    if result.get("status") not in ("running", "stopped"):
        raise HTTPException(status_code=503, detail="Unexpected workload state")
    return {"registered": True, "status": result["status"],
            "url": salamander_url(identity)}


@app.post("/v1/salamander/start")
def gateway_salamander_start(request: Request):
    identity, workload_id = salamander_workload_for(request)
    if workload_id is None:
        raise HTTPException(status_code=404, detail="Salamander not registered")
    result = salamander_agent_call("workload_start", workload_id)
    return {"registered": True, "status": result.get("status"),
            "url": salamander_url(identity)}


@app.post("/v1/salamander/stop")
def gateway_salamander_stop(request: Request):
    identity, workload_id = salamander_workload_for(request)
    if workload_id is None:
        raise HTTPException(status_code=404, detail="Salamander not registered")
    result = salamander_agent_call("workload_stop", workload_id)
    return {"registered": True, "status": result.get("status"),
            "url": salamander_url(identity)}
