BEGIN;

ALTER TABLE workload_gpus
    ADD CONSTRAINT workload_gpus_device_unique
    UNIQUE (gpu_device_id);

INSERT INTO schema_migrations(version)
VALUES (18);

COMMIT;
