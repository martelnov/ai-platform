BEGIN;

ALTER TABLE user_connectors
    ADD COLUMN provider_id text,
    ADD COLUMN dns_name text;

CREATE UNIQUE INDEX user_connectors_provider_id_unique
    ON user_connectors(connector_type, provider_id)
    WHERE provider_id IS NOT NULL;

INSERT INTO schema_migrations(version)
VALUES (7);

COMMIT;
