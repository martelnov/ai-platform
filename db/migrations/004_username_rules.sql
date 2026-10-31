BEGIN;

ALTER TABLE users
    ADD CONSTRAINT users_username_format_check
    CHECK (
        username = lower(username)
        AND username ~ '^[a-z][a-z0-9._-]{0,63}$'
    );

INSERT INTO schema_migrations(version)
VALUES (4);

COMMIT;
