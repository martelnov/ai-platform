-- AI Platform migration 014
-- Update SalamanderRS to the tested image with security fixes and SIGTERM support.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM schema_migrations WHERE version = 13
    ) THEN
        RAISE EXCEPTION 'Migration 013 must be applied before 014';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM applications WHERE code = 'salamander'
    ) THEN
        RAISE EXCEPTION 'SalamanderRS application is not registered';
    END IF;
END;
$$;

UPDATE applications
SET image = 'ai-platform-salamander:0.10.0-e7dd497'
WHERE code = 'salamander'
  AND image IN (
      'ai-platform-salamander:0.10.0-7804126',
      'ai-platform-salamander:0.10.0-sigterm'
  );

INSERT INTO schema_migrations (version) VALUES (14);

COMMIT;
