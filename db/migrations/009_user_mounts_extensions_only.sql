BEGIN;

DELETE FROM user_mounts
WHERE mount_path = '/'
  AND scope = 'platform'
  AND source_type = 'platform'
  AND source_ref IN (
      'bootstrap:data_root',
      'bootstrap:models_root'
  );

ALTER TABLE user_mounts
    DROP CONSTRAINT user_mounts_check;

ALTER TABLE user_mounts
    ADD CONSTRAINT user_mounts_mount_path_not_root
    CHECK (mount_path <> '/');

INSERT INTO schema_migrations(version)
VALUES (9);

COMMIT;
