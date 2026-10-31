BEGIN;

CREATE TABLE platform_admins (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    tailscale_login     text NOT NULL UNIQUE,
    display_name        text,

    enabled             boolean NOT NULL DEFAULT true,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz,

    CHECK (
        tailscale_login = lower(tailscale_login)
        AND length(tailscale_login) BETWEEN 3 AND 254
    )
);

INSERT INTO schema_migrations(version)
VALUES (6);

COMMIT;
