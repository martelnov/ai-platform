BEGIN;

CREATE TABLE reservations (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         bigint NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE,

    starts_at       timestamptz NOT NULL,
    ends_at         timestamptz NOT NULL,

    gpu_count       integer NOT NULL DEFAULT 1,

    status          text NOT NULL DEFAULT 'scheduled',

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (ends_at > starts_at),
    CHECK (gpu_count > 0),

    CHECK (status IN (
        'scheduled',
        'active',
        'completed',
        'cancelled'
    ))
);

CREATE INDEX idx_reservations_time
    ON reservations(starts_at, ends_at);

CREATE INDEX idx_reservations_user
    ON reservations(user_id);

CREATE TABLE workloads (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    user_id                 bigint NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,

    host_id                 bigint NOT NULL
                            REFERENCES hosts(id) ON DELETE RESTRICT,

    application_profile_id  bigint NOT NULL
                            REFERENCES application_profiles(id)
                            ON DELETE RESTRICT,

    connector_id            bigint
                            REFERENCES user_connectors(id)
                            ON DELETE RESTRICT,

    reservation_id          bigint
                            REFERENCES reservations(id)
                            ON DELETE SET NULL,

    mode                    text NOT NULL,

    status                  text NOT NULL DEFAULT 'pending',

    container_name          text,

    created_at              timestamptz NOT NULL DEFAULT now(),
    started_at              timestamptz,
    stopped_at              timestamptz,
    updated_at              timestamptz NOT NULL DEFAULT now(),

    last_error              text,

    CHECK (mode IN (
        'preparation',
        'gpu'
    )),

    CHECK (status IN (
        'pending',
        'starting',
        'running',
        'stopping',
        'stopped',
        'failed'
    ))
);

CREATE INDEX idx_workloads_user
    ON workloads(user_id);

CREATE INDEX idx_workloads_status
    ON workloads(status);

CREATE TABLE workload_gpus (
    workload_id     bigint NOT NULL
                    REFERENCES workloads(id) ON DELETE CASCADE,

    gpu_device_id   bigint NOT NULL
                    REFERENCES gpu_devices(id) ON DELETE RESTRICT,

    allocated_at    timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (workload_id, gpu_device_id)
);

CREATE INDEX idx_workload_gpus_device
    ON workload_gpus(gpu_device_id);

INSERT INTO schema_migrations(version)
VALUES (2);

COMMIT;
