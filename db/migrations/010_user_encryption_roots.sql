BEGIN;

CREATE TABLE user_encryption_roots (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             bigint NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,

    tree                text NOT NULL
                            CHECK (tree IN ('data', 'models')),

    root_path           text NOT NULL,

    encryption_type     text NOT NULL DEFAULT 'fscrypt'
                            CHECK (encryption_type = 'fscrypt'),

    enabled             boolean NOT NULL DEFAULT true,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (user_id, tree, root_path),

    CHECK (
        root_path ~ '^/([^/]+(/[^/]+)*)?$'
    ),

    CHECK (
        root_path !~ '(^|/)(\.|\.\.)(/|$)'
    ),

    CHECK (
        root_path <> '/'
    )
);

CREATE INDEX user_encryption_roots_user_idx
    ON user_encryption_roots(user_id)
    WHERE enabled = true;


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
          OR strpos(
              NEW.root_path,
              um.mount_path || '/'
          ) = 1
      )
    LIMIT 1;

    IF conflict_path IS NOT NULL THEN
        RAISE EXCEPTION
            'encryption root % overlaps mount %',
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


CREATE TRIGGER user_encryption_roots_overlap_trg
BEFORE INSERT OR UPDATE OF
    user_id,
    tree,
    root_path,
    enabled
ON user_encryption_roots
FOR EACH ROW
EXECUTE FUNCTION check_user_encryption_root_overlap();


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
          er.root_path = NEW.mount_path
          OR strpos(
              er.root_path,
              NEW.mount_path || '/'
          ) = 1
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


CREATE TRIGGER user_mounts_encryption_overlap_trg
BEFORE INSERT OR UPDATE OF
    user_id,
    tree,
    mount_path,
    enabled
ON user_mounts
FOR EACH ROW
EXECUTE FUNCTION check_user_mount_encryption_overlap();


INSERT INTO schema_migrations(version)
VALUES (10);

COMMIT;
