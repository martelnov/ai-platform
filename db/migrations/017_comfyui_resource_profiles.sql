-- AI Platform migration 017: universal ComfyUI resource-profile foundation.
-- SCHEMA / DISABLED PROFILES ONLY. Does not change the active image, existing
-- cpu-test profile, workload assignments, or running containers.
-- Activate new profiles only after helper, agent, allocator, and installer
-- support have been deployed and validated together.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = 16) THEN
        RAISE EXCEPTION 'Migration 016 must be applied before 017';
    END IF;
    IF EXISTS (SELECT 1 FROM schema_migrations WHERE version = 17) THEN
        RAISE EXCEPTION 'Migration 017 already applied';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM applications WHERE code = 'comfyui'
    ) THEN
        RAISE EXCEPTION 'ComfyUI application missing';
    END IF;
END;
$$;

-- Explicit, host-specific CPU pools. All CPU numbers are Linux logical CPUs.
-- The agent must expand physical cores to their SMT siblings when exclusive
-- isolation is requested, and must validate that masks are online/disjoint.
-- No guessed CPU masks are installed here: the final server CPU is not fitted.
CREATE TABLE host_cpu_pools (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id         bigint NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    name            text NOT NULL
                    CHECK (name IN ('system', 'preparation', 'gpu')),
    cpuset_cpus     text NOT NULL,
    enabled         boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (host_id, name),
    UNIQUE (id, host_id),
    CHECK (cpuset_cpus ~ '^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$')
);

-- Per-host, per-application-profile runtime settings. This enables the admin
-- UI to configure CPU/RAM/allowed cores for 0, 1, and 2 GPU modes without
-- hardcoding host-dependent CPU indexes in Python.
-- cpuset_cpus: optional subset of host_cpu_pools.cpuset_cpus; NULL = whole pool.
-- cpu_allocation='shared': every workload shares the allowed pool (prep).
-- cpu_allocation='exclusive': allocator chooses non-overlapping physical cores
-- and all their sibling threads from the allowed pool (GPU workloads).
CREATE TABLE host_application_profile_resources (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id                 bigint NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    application_profile_id  bigint NOT NULL
                            REFERENCES application_profiles(id) ON DELETE CASCADE,
    cpu_pool_id             bigint NOT NULL,
    cpu_allocation          text NOT NULL
                            CHECK (cpu_allocation IN ('shared', 'exclusive')),
    cpuset_cpus             text,
    cpu_limit               numeric(6,2) NOT NULL CHECK (cpu_limit > 0),
    memory_limit_bytes      bigint NOT NULL CHECK (memory_limit_bytes >= 268435456),
    enabled                 boolean NOT NULL DEFAULT false,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (host_id, application_profile_id),
    FOREIGN KEY (cpu_pool_id, host_id)
        REFERENCES host_cpu_pools(id, host_id) ON DELETE RESTRICT,
    CHECK (cpuset_cpus IS NULL OR
           cpuset_cpus ~ '^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$')
);
CREATE INDEX idx_host_profile_resources_pool
    ON host_application_profile_resources(cpu_pool_id);

-- Register upcoming modes without exposing them to users. The old cpu-test
-- profile stays unchanged until the universal runtime is fully integrated.
INSERT INTO application_profiles (
    application_id, name, requires_reservation,
    cpu_limit, memory_limit_bytes, gpu_min, gpu_max,
    command, environment, config, enabled
)
SELECT a.id, profiles.name, profiles.requires_reservation,
       profiles.cpu_limit, profiles.memory_limit_bytes,
       profiles.gpu_min, profiles.gpu_max,
       NULL,
       '{"HOME":"/workspace","XDG_CACHE_HOME":"/workspace/.cache"}'::jsonb,
       profiles.config,
       false
FROM applications a
CROSS JOIN (
    VALUES
      ('cpu-prep'::text, false, 1.00::numeric, 4294967296::bigint,
       0, 0, '{"cpu_pool":"preparation","pids_limit":256,"purpose":"preparation"}'::jsonb),
      ('gpu-1'::text, true, 4.00::numeric, 17179869184::bigint,
       1, 1, '{"cpu_pool":"gpu","pids_limit":256,"purpose":"generation"}'::jsonb),
      ('gpu-2'::text, true, 8.00::numeric, 34359738368::bigint,
       2, 2, '{"cpu_pool":"gpu","pids_limit":256,"purpose":"generation"}'::jsonb)
) AS profiles(name, requires_reservation, cpu_limit,
             memory_limit_bytes, gpu_min, gpu_max, config)
WHERE a.code = 'comfyui';

-- Deliberately do NOT update applications.image or applications.config,
-- rename cpu-test, move any existing workload, or enable the GPU profiles.
INSERT INTO schema_migrations(version) VALUES (17);
COMMIT;
