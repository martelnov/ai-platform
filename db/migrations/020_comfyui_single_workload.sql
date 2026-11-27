BEGIN;
LOCK TABLE workloads, application_profiles, applications, workload_gpus IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE
    prep bigint;
    profile_ids text;
BEGIN
    SELECT p.id INTO STRICT prep FROM application_profiles p JOIN applications a ON a.id=p.application_id WHERE a.code='comfyui' AND p.name='cpu-prep';
    IF EXISTS (SELECT 1 FROM workloads w JOIN application_profiles p ON p.id=w.application_profile_id JOIN applications a ON a.id=p.application_id WHERE a.code='comfyui' AND (w.status NOT IN ('stopped','pending','failed') OR EXISTS (SELECT 1 FROM workload_gpus g WHERE g.workload_id=w.id))) THEN
        RAISE EXCEPTION 'Stop all ComfyUI workloads and release GPUs before migration 020';
    END IF;
    -- Prefer the existing universal workload; discard only obsolete CPU test rows.
    DELETE FROM workloads w USING application_profiles p, applications a
    WHERE w.application_profile_id=p.id AND p.application_id=a.id AND a.code='comfyui' AND p.name='cpu-test'
      AND EXISTS (SELECT 1 FROM workloads other JOIN application_profiles op ON op.id=other.application_profile_id WHERE other.user_id=w.user_id AND op.application_id=a.id AND op.name<>'cpu-test');
    UPDATE workloads w SET application_profile_id=prep, mode='preparation', reservation_id=NULL, container_name=NULL, status='stopped', last_error=NULL, updated_at=now()
    FROM application_profiles p, applications a WHERE w.application_profile_id=p.id AND p.application_id=a.id AND a.code='comfyui' AND p.name='cpu-test';
    DELETE FROM application_profiles p USING applications a WHERE p.application_id=a.id AND a.code='comfyui' AND p.name='cpu-test';
    UPDATE applications SET image='ai-platform-comfyui:universal' WHERE code='comfyui';
    UPDATE application_profiles p SET enabled=true FROM applications a WHERE p.application_id=a.id AND a.code='comfyui' AND p.name='cpu-prep';
    SELECT string_agg(p.id::text, ',' ORDER BY p.id) INTO profile_ids FROM application_profiles p JOIN applications a ON a.id=p.application_id WHERE a.code='comfyui';
    -- Covers all registered ComfyUI profiles; extend this index if adding profiles.
    EXECUTE format('CREATE UNIQUE INDEX workloads_comfyui_one_per_user ON workloads(user_id) WHERE application_profile_id IN (%s)', profile_ids);
END $$;
INSERT INTO schema_migrations(version) VALUES (20);
COMMIT;
