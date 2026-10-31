BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version         integer PRIMARY KEY,
    applied_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hosts (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    hostname        text,
    enabled         boolean NOT NULL DEFAULT true,
    labels          jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username        text NOT NULL UNIQUE,
    display_name    text,
    email           text,
    linux_uid       integer NOT NULL UNIQUE,
    linux_gid       integer NOT NULL UNIQUE,
    enabled         boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (linux_uid >= 1000),
    CHECK (linux_gid >= 1000)
);

CREATE TABLE IF NOT EXISTS applications (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code            text NOT NULL UNIQUE,
    name            text NOT NULL,
    image           text NOT NULL,
    ports           jsonb NOT NULL DEFAULT '[]'::jsonb,
    enabled         boolean NOT NULL DEFAULT true,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS application_profiles (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    application_id      bigint NOT NULL
                        REFERENCES applications(id) ON DELETE CASCADE,

    name                text NOT NULL,
    requires_reservation boolean NOT NULL DEFAULT true,

    cpu_limit           numeric(6,2),
    memory_limit_bytes  bigint,

    gpu_min             integer NOT NULL DEFAULT 0,
    gpu_max             integer NOT NULL DEFAULT 0,

    command             jsonb,
    environment         jsonb NOT NULL DEFAULT '{}'::jsonb,
    config              jsonb NOT NULL DEFAULT '{}'::jsonb,

    enabled             boolean NOT NULL DEFAULT true,

    UNIQUE (application_id, name),

    CHECK (cpu_limit IS NULL OR cpu_limit > 0),
    CHECK (memory_limit_bytes IS NULL OR memory_limit_bytes > 0),
    CHECK (gpu_min >= 0),
    CHECK (gpu_max >= gpu_min)
);

CREATE TABLE IF NOT EXISTS gpu_devices (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id             bigint NOT NULL
                        REFERENCES hosts(id) ON DELETE CASCADE,

    gpu_uuid            text NOT NULL UNIQUE,
    runtime_index       integer,
    name                text NOT NULL,
    memory_total_bytes  bigint,

    pci_bus_id          text,
    numa_node           integer,
    pcie_generation     integer,
    pcie_width          integer,
    compute_capability  text,

    enabled             boolean NOT NULL DEFAULT true,
    present             boolean NOT NULL DEFAULT true,
    last_seen_at        timestamptz NOT NULL DEFAULT now(),

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CHECK (memory_total_bytes IS NULL OR memory_total_bytes > 0),
    CHECK (pcie_generation IS NULL OR pcie_generation > 0),
    CHECK (pcie_width IS NULL OR pcie_width > 0)
);

CREATE INDEX IF NOT EXISTS idx_gpu_devices_host
    ON gpu_devices(host_id);

CREATE TABLE IF NOT EXISTS storage_resources (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id         bigint
                    REFERENCES hosts(id) ON DELETE RESTRICT,

    name            text NOT NULL UNIQUE,
    backend_type    text NOT NULL,
    source_ref      text NOT NULL,
    mount_point     text,

    secret_ref      text,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,

    enabled         boolean NOT NULL DEFAULT true,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_storage_assignments (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             bigint NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,

    storage_resource_id bigint NOT NULL
                        REFERENCES storage_resources(id) ON DELETE RESTRICT,

    role                text NOT NULL,
    container_path      text,

    enabled             boolean NOT NULL DEFAULT true,

    UNIQUE (user_id, role)
);

CREATE TABLE IF NOT EXISTS filesystem_quotas (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    user_id                 bigint NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,

    storage_resource_id     bigint NOT NULL
                            REFERENCES storage_resources(id) ON DELETE CASCADE,

    soft_bytes              bigint,
    hard_bytes              bigint,

    effective_soft_bytes    bigint,
    effective_hard_bytes    bigint,

    last_applied_at         timestamptz,
    last_checked_at         timestamptz,
    last_error              text,

    UNIQUE (user_id, storage_resource_id),

    CHECK (soft_bytes IS NULL OR soft_bytes >= 0),
    CHECK (hard_bytes IS NULL OR hard_bytes >= 0),
    CHECK (
        soft_bytes IS NULL OR
        hard_bytes IS NULL OR
        hard_bytes >= soft_bytes
    )
);

CREATE TABLE IF NOT EXISTS user_connectors (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         bigint NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE,

    connector_type  text NOT NULL,
    node_name       text NOT NULL,
    state_path      text NOT NULL,

    enabled         boolean NOT NULL DEFAULT true,
    status          text NOT NULL DEFAULT 'unknown',

    address         text,
    last_seen_at    timestamptz,
    last_error      text,

    config          jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (user_id, connector_type),
    UNIQUE (connector_type, node_name)
);

INSERT INTO schema_migrations(version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;

COMMIT;
