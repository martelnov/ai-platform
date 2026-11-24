-- AI Platform migration 016
-- Register ComfyUI CPU test image and profile. This does NOT create any
-- user workload, start a container, or enable GPU allocation.
-- Actual workload lifecycle must be implemented in host-agent and Gateway.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM schema_migrations WHERE version = 15
    ) THEN
        RAISE EXCEPTION 'Migration 015 must be applied before 016';
    END IF;
    IF EXISTS (
        SELECT 1 FROM schema_migrations WHERE version = 16
    ) THEN
        RAISE EXCEPTION 'Migration 016 already applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM applications WHERE code = 'comfyui'
    ) THEN
        RAISE EXCEPTION 'ComfyUI already registered: review before migration';
    END IF;
END;
$$;

INSERT INTO applications (
    code, name, image, ports, enabled, config
) VALUES (
    'comfyui',
    'ComfyUI',
    'ai-platform-comfyui:cpu-test',
    '[8188]'::jsonb,
    true,
    '{
        "kind": "interactive-ai",
        "start_policy": "on_demand",
        "single_instance_per_user": true,
        "cpu_only": true,
        "workdir": "/workspace",
        "models_user": "/models/user",
        "models_common": "/models/common",
        "models_common_read_only": true
    }'::jsonb
);

INSERT INTO application_profiles (
    application_id, name, requires_reservation,
    cpu_limit, memory_limit_bytes, gpu_min, gpu_max,
    command, environment, config, enabled
)
SELECT
    a.id,
    'cpu-test',
    false,
    4.00,
    17179869184,
    0,
    0,
    NULL,
    '{
        "HOME": "/workspace",
        "XDG_CACHE_HOME": "/workspace/.cache"
    }'::jsonb,
    '{
        "cpu_pool": "interactive",
        "memory_swap_bytes": 17179869184,
        "pids_limit": 256,
        "purpose": "integration-test"
    }'::jsonb,
    true
FROM applications a
WHERE a.code = 'comfyui';

INSERT INTO schema_migrations(version) VALUES (16);
COMMIT;
