BEGIN;

UPDATE applications
SET config = config - 'cpu_only'
WHERE code = 'comfyui';

INSERT INTO schema_migrations(version)
VALUES (19);

COMMIT;
