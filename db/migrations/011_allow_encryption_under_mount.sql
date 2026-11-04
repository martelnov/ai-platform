BEGIN;

CREATE OR REPLACE FUNCTION check_user_encryption_root_overlap()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    conflict_path text;
BEGIN
    IF NOT NEW.enabled THEN
        RETURN NEW;
    END IF;

    SELECT um.mount_path
    INTO conflict_path
    FROM user_mounts um
    WHERE um.user_id = NEW.user_id
      AND um.tree = NEW.tree
      AND um.enabled = true
      AND (
          um.mount_path = NEW.root_path
          OR strpos(
              um.mount_path,
              NEW.root_path || '/'
          ) = 1
      )
    LIMIT 1;

    IF conflict_path IS NOT NULL THEN
        RAISE EXCEPTION
            'encryption root % conflicts with mount %',
            NEW.root_path,
            conflict_path;
    END IF;

    conflict_path := NULL;

    SELECT er.root_path
    INTO conflict_path
    FROM user_encryption_roots er
    WHERE er.user_id = NEW.user_id
      AND er.tree = NEW.tree
      AND er.enabled = true
      AND (
          NEW.id IS NULL
          OR er.id <> NEW.id
      )
      AND (
          er.root_path = NEW.root_path
          OR strpos(
              er.root_path,
              NEW.root_path || '/'
          ) = 1
          OR strpos(
              NEW.root_path,
              er.root_path || '/'
          ) = 1
      )
    LIMIT 1;

    IF conflict_path IS NOT NULL THEN
        RAISE EXCEPTION
            'encryption root % overlaps encryption root %',
            NEW.root_path,
            conflict_path;
    END IF;

    RETURN NEW;
END;
$$;

INSERT INTO schema_migrations(version)
VALUES (11);

COMMIT;
