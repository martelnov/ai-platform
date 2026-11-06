-- AI Platform migration 013
-- Register SalamanderRS as an on-demand utility application.
-- Resource and startup metadata are stored here; runtime enforcement is a separate step.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM schema_migrations WHERE version = 12
    ) THEN
        RAISE EXCEPTION 'Migration 012 must be applied before 013';
    END IF;
END;
$$;

-- An auxiliary application uses neither a GPU reservation nor the
-- limited CPU preparation mode.
ALTER TABLE workloads
    DROP CONSTRAINT workloads_mode_check;
ALTER TABLE workloads
    ADD CONSTRAINT workloads_mode_check
    CHECK (mode IN ('preparation', 'gpu', 'utility'));

INSERT INTO applications (
    code, name, image, ports, enabled, config
)
VALUES (
    'salamander',
    'SalamanderRS Web',
    'ai-platform-salamander:0.10.0-7804126',
    '[8090]'::jsonb,
    true,
    '{
        "kind": "utility",
        "start_policy": "on_demand",
        "single_instance_per_user": true,
        "data_root": "/data",
        "models_root": "/models",
        "include_common": false
    }'::jsonb
)
ON CONFLICT (code) DO NOTHING;

INSERT INTO application_profiles (
    application_id, name, requires_reservation,
    cpu_limit, memory_limit_bytes,
    gpu_min, gpu_max,
    command, environment, config, enabled
)
SELECT
    a.id,
    'default',
    false,
    1.00,
    536870912,
    0,
    0,
    NULL,
    '{
        "SALAMANDER_DATA_ROOT": "/data",
        "SALAMANDER_MODELS_ROOT": "/models",
        "SALAMANDER_ENABLE_WRITES": "1",
        "SALAMANDER_PORT": "8090"
    }'::jsonb,
    '{
        "cpu_pool": "interactive",
        "memory_swap_bytes": 536870912,
        "pids_limit": 128
    }'::jsonb,
    true
FROM applications AS a
WHERE a.code = 'salamander'
ON CONFLICT (application_id, name) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES (13);

COMMIT;
