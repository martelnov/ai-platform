BEGIN;

CREATE TABLE platform_secrets (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    name            text NOT NULL UNIQUE,
    description     text,

    algorithm       text NOT NULL DEFAULT 'aes-256-gcm',
    key_version     integer NOT NULL DEFAULT 1,

    nonce           bytea NOT NULL,
    ciphertext      bytea NOT NULL,

    expires_at      timestamptz,

    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (name ~ '^[a-z][a-z0-9._-]{0,127}$'),
    CHECK (algorithm = 'aes-256-gcm'),
    CHECK (key_version > 0),
    CHECK (octet_length(nonce) = 12)
);

INSERT INTO schema_migrations(version)
VALUES (5);

COMMIT;
