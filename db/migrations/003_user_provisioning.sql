BEGIN;

CREATE SEQUENCE IF NOT EXISTS linux_identity_seq
    AS integer
    START WITH 20000
    INCREMENT BY 1
    MINVALUE 20000;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS provisioning_status text
        NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS provisioned_at timestamptz,
    ADD COLUMN IF NOT EXISTS provisioning_error text;

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_provisioning_status_check;

ALTER TABLE users
    ADD CONSTRAINT users_provisioning_status_check
    CHECK (provisioning_status IN (
        'pending',
        'provisioning',
        'ready',
        'failed'
    ));

INSERT INTO schema_migrations(version)
VALUES (3);

COMMIT;
