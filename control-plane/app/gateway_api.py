from fastapi import FastAPI, HTTPException, Request

from app.db import db_connect


app = FastAPI(
    title="AI Platform Gateway API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "gateway-api",
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
