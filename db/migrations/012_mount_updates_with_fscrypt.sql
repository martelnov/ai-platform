BEGIN;

CREATE OR REPLACE FUNCTION check_user_mount_encryption_overlap()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    conflict_path text;
BEGIN
    IF NOT NEW.enabled THEN
        RETURN NEW;
    END IF;

    SELECT er.root_path
    INTO conflict_path
    FROM user_encryption_roots er
    WHERE er.user_id = NEW.user_id
      AND er.tree = NEW.tree
      AND er.enabled = true
      AND (
          NEW.mount_path = er.root_path
          OR strpos(
              NEW.mount_path,
              er.root_path || '/'
          ) = 1
      )
    LIMIT 1;

    IF conflict_path IS NOT NULL THEN
        RAISE EXCEPTION
            'mount % overlaps encryption root %',
            NEW.mount_path,
            conflict_path;
    END IF;

    RETURN NEW;
END;
$$;

INSERT INTO schema_migrations(version)
VALUES (12);

COMMIT;
