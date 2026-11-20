-- AI Platform migration 015: non-destructive GPU retirement and audit trail.
-- A retired GPU is never reservation capacity. Do not delete its inventory row.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = 14) THEN
        RAISE EXCEPTION 'Migration 014 must be applied before 015';
    END IF;
    IF EXISTS (SELECT 1 FROM schema_migrations WHERE version = 15) THEN
        RAISE EXCEPTION 'Migration 015 is already applied';
    END IF;
END;
$$;

ALTER TABLE gpu_devices
    ADD COLUMN retired_at timestamptz;

-- Defence in depth: even an older enable endpoint cannot enable a retired GPU.
-- 'present' stays independent of administrative retirement.
ALTER TABLE gpu_devices
    ADD CONSTRAINT gpu_devices_retired_must_be_disabled
    CHECK (retired_at IS NULL OR enabled = false);

-- Keep retired slot addresses for history, while allowing a replacement
-- adapter in that PCI slot. At most one non-retired record may claim a slot.
CREATE UNIQUE INDEX idx_gpu_devices_active_host_pci
    ON gpu_devices (host_id, lower(pci_bus_id))
    WHERE retired_at IS NULL AND pci_bus_id IS NOT NULL;

CREATE TABLE gpu_device_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gpu_id          bigint NOT NULL REFERENCES gpu_devices(id) ON DELETE RESTRICT,
    action          text NOT NULL CHECK (action IN ('retire', 'restore')),
    admin_login     text NOT NULL CHECK (length(btrim(admin_login)) > 0),
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_gpu_device_events_gpu_time
    ON gpu_device_events(gpu_id, occurred_at DESC, id DESC);

INSERT INTO schema_migrations (version) VALUES (15);
COMMIT;
