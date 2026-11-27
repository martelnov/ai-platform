#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG_FILE = "/etc/ai-platform/config.toml"
COMPOSE_ENV_FILE = "/etc/ai-platform/compose.env"

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


def die(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def load_config():
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def psql(sql):
    cfg = load_config()
    compose_file = str(
        Path(cfg["platform"]["repo_root"]) / "compose.yml"
    )

    cmd = [
        "docker", "compose",
        "--env-file", COMPOSE_ENV_FILE,
        "-f", compose_file,
        "exec", "-T",
        "postgres",
        "psql",
        "-X", "-A", "-t",
        "-v", "ON_ERROR_STOP=1",
        "-U", "ai_platform",
        "-d", "ai_platform",
        "-c", sql,
    ]

    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())

    return result.stdout.strip()


def sql_text(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql_num(value):
    if value is None:
        return "NULL"
    return str(int(value))


def get_user(user_id):
    raw = psql(
        f"""
        SELECT json_build_object(
            'id', id,
            'username', username,
            'uid', linux_uid,
            'gid', linux_gid,
            'enabled', enabled
        )::text
        FROM users
        WHERE id = {user_id};
        """
    )

    if not raw:
        die(f"user id {user_id} does not exist")

    return json.loads(raw)


def get_quotas(user_id):
    raw = psql(
        f"""
        SELECT COALESCE(
            json_agg(
                json_build_object(
                    'quota_id', fq.id,
                    'storage_name', s.name,
                    'source_ref', s.source_ref,
                    'mount_point', s.mount_point,
                    'soft_bytes', fq.soft_bytes,
                    'hard_bytes', fq.hard_bytes,
                    'effective_soft_bytes', fq.effective_soft_bytes,
                    'effective_hard_bytes', fq.effective_hard_bytes
                )
                ORDER BY s.id
            ),
            '[]'::json
        )::text
        FROM filesystem_quotas fq
        JOIN storage_resources s
          ON s.id = fq.storage_resource_id
        WHERE fq.user_id = {user_id}
          AND s.enabled = true;
        """
    )

    return json.loads(raw)


def workload_describe(workload_id):
    """Return the authoritative runtime specification from PostgreSQL."""

    raw = psql(
        f"""
        SELECT json_build_object(
            'id', w.id,
            'mode', w.mode,
            'reservation_id', w.reservation_id,
            'reservation', (
                SELECT json_build_object(
                    'id', r.id,
                    'user_id', r.user_id,
                    'gpu_count', r.gpu_count,
                    'starts_at', r.starts_at,
                    'ends_at', r.ends_at,
                    'status', r.status,
                    'valid_now', (
                        r.starts_at <= now()
                        AND r.ends_at > now()
                        AND r.status IN ('scheduled', 'active')
                    )
                )
                FROM reservations r
                WHERE r.id = w.reservation_id
            ),
            'status', w.status,
            'host_name', h.name,
            'host_id', h.id,
            'user_id', u.id,
            'username', u.username,
            'uid', u.linux_uid,
            'gid', u.linux_gid,
            'user_enabled', u.enabled,
            'provisioning_status', u.provisioning_status,
            'connector_id', uc.id,
            'connector_enabled', uc.enabled,
            'connector_status', uc.status,
            'application_code', a.code,
            'image', CASE
                WHEN a.code = 'comfyui' AND ap.name IN ('cpu-prep', 'gpu-1', 'gpu-2')
                    THEN 'ai-platform-comfyui:universal'
                ELSE a.image
            END,
            'application_enabled', a.enabled,
            'application_config', a.config,
            'profile_name', ap.name,
            'profile_enabled', ap.enabled,
            'requires_reservation', ap.requires_reservation,
            'resource_override_enabled', COALESCE(hpr.enabled, false),
            'cpu_limit', CASE WHEN hpr.enabled
                THEN hpr.cpu_limit ELSE ap.cpu_limit END,
            'memory_limit_bytes', CASE WHEN hpr.enabled
                THEN hpr.memory_limit_bytes ELSE ap.memory_limit_bytes END,
            'cpuset_cpus', CASE WHEN hpr.enabled
                THEN hpr.cpuset_cpus ELSE NULL END,
            'gpu_min', ap.gpu_min,
            'gpu_max', ap.gpu_max,
            'allocated_gpus', (
                SELECT COALESCE(json_agg(json_build_object(
                    'id', gd.id,
                    'gpu_uuid', gd.gpu_uuid,
                    'host_id', gd.host_id,
                    'enabled', gd.enabled,
                    'present', gd.present
                ) ORDER BY gd.id), '[]'::json)
                FROM workload_gpus wg
                JOIN gpu_devices gd ON gd.id = wg.gpu_device_id
                WHERE wg.workload_id = w.id
            ),
            'command', ap.command,
            'environment', ap.environment,
            'profile_config', ap.config,
            'mounts', (
                SELECT COALESCE(json_agg(json_build_object(
                    'tree', um.tree,
                    'mount_path', um.mount_path,
                    'scope', um.scope,
                    'source_type', um.source_type,
                    'source_ref', um.source_ref
                ) ORDER BY um.tree, um.mount_path), '[]'::json)
                FROM user_mounts um
                WHERE um.user_id = u.id
                  AND um.enabled = true
                  AND um.mount_path <> '/'
            ),
            'encryption_roots', (
                SELECT COALESCE(json_agg(json_build_object(
                    'tree', er.tree,
                    'root_path', er.root_path
                ) ORDER BY er.tree, er.root_path), '[]'::json)
                FROM user_encryption_roots er
                WHERE er.user_id = u.id
                  AND er.enabled = true
            )
        )::text
        FROM workloads w
        JOIN hosts h ON h.id = w.host_id
        JOIN users u ON u.id = w.user_id
        JOIN user_connectors uc ON uc.id = w.connector_id
                            AND uc.user_id = u.id
        JOIN application_profiles ap ON ap.id = w.application_profile_id
        LEFT JOIN host_application_profile_resources hpr
          ON hpr.host_id = w.host_id
         AND hpr.application_profile_id = ap.id
        JOIN applications a ON a.id = ap.application_id
        WHERE w.id = {workload_id};
        """
    )

    if not raw:
        die(f"workload {workload_id} not found or incomplete")

    print(raw)


def workload_allocate_gpus(workload_id):
    """Allocate physical GPUs under the reservation transaction lock."""

    workload_id = int(workload_id)
    if workload_id <= 0:
        die("invalid workload id")

    node = load_config()["node"]["name"]

    if not isinstance(node, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", node
    ):
        die("invalid node name")

    psql(
        f"""
        BEGIN;

        SELECT pg_advisory_xact_lock(104912, 1);

        DO $allocation$
        DECLARE
            target RECORD;
            requested integer;
            allocated integer;
            used_count integer;
        BEGIN
            SELECT
                w.id,
                w.host_id,
                w.user_id,
                w.status,
                w.mode,
                w.reservation_id,
                ap.name AS profile_name,
                ap.gpu_min,
                ap.gpu_max,
                ap.requires_reservation,
                r.user_id AS reservation_user_id,
                r.gpu_count AS reserved_count,
                r.starts_at,
                r.ends_at,
                r.status AS reservation_status
            INTO target
            FROM workloads w
            JOIN hosts h ON h.id = w.host_id
            JOIN application_profiles ap
                ON ap.id = w.application_profile_id
            JOIN applications a ON a.id = ap.application_id
            JOIN users u ON u.id = w.user_id
            JOIN reservations r ON r.id = w.reservation_id
            WHERE w.id = {workload_id}
              AND h.name = {sql_text(node)}
              AND a.code = 'comfyui'
              AND a.enabled
              AND ap.enabled
              AND u.enabled
            FOR UPDATE OF w, r;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'GPU workload not available';
            END IF;

            IF target.mode <> 'gpu'
               OR target.requires_reservation IS NOT TRUE
               OR target.profile_name NOT IN ('gpu-1', 'gpu-2')
            THEN
                RAISE EXCEPTION 'Invalid GPU workload profile';
            END IF;

            requested := CASE target.profile_name
                WHEN 'gpu-1' THEN 1
                WHEN 'gpu-2' THEN 2
            END;

            IF target.gpu_min <> requested
               OR target.gpu_max <> requested
            THEN
                RAISE EXCEPTION 'Invalid GPU profile capacity';
            END IF;

            IF target.status NOT IN ('pending', 'stopped', 'failed')
            THEN
                RAISE EXCEPTION 'Workload is not stopped';
            END IF;

            IF target.reservation_user_id <> target.user_id
               OR target.reservation_status NOT IN ('scheduled', 'active')
               OR target.starts_at > clock_timestamp()
               OR target.ends_at <= clock_timestamp()
            THEN
                RAISE EXCEPTION 'GPU reservation is not valid';
            END IF;

            IF EXISTS (
                SELECT 1 FROM workload_gpus
                WHERE workload_id = target.id
            ) THEN
                RAISE EXCEPTION 'Workload already holds GPUs';
            END IF;

            SELECT count(*) INTO used_count
            FROM workload_gpus wg
            JOIN workloads w ON w.id = wg.workload_id
            WHERE w.reservation_id = target.reservation_id;

            IF used_count + requested > target.reserved_count THEN
                RAISE EXCEPTION 'Reservation GPU capacity exceeded';
            END IF;

            INSERT INTO workload_gpus (
                workload_id, gpu_device_id
            )
            SELECT target.id, gd.id
            FROM gpu_devices gd
            WHERE gd.host_id = target.host_id
              AND gd.enabled = true
              AND gd.present = true
              AND gd.gpu_uuid ~
                  '^GPU-[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$'
              AND NOT EXISTS (
                  SELECT 1 FROM workload_gpus wg
                  WHERE wg.gpu_device_id = gd.id
              )
            ORDER BY gd.id
            LIMIT requested;

            GET DIAGNOSTICS allocated = ROW_COUNT;

            IF allocated <> requested THEN
                RAISE EXCEPTION 'Insufficient free GPUs on host';
            END IF;

            UPDATE workloads
            SET status = 'starting',
                updated_at = now(),
                last_error = NULL
            WHERE id = target.id;
        END
        $allocation$;

        COMMIT;
        """
    )


def workload_release_gpus(workload_id):
    """Release GPUs after the agent has confirmed container termination."""

    workload_id = int(workload_id)
    if workload_id <= 0:
        die("invalid workload id")

    node = load_config()["node"]["name"]

    if not isinstance(node, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", node
    ):
        die("invalid node name")

    psql(
        f"""
        BEGIN;

        SELECT pg_advisory_xact_lock(104912, 1);

        DO $release$
        DECLARE
            target RECORD;
        BEGIN
            SELECT w.id, w.status, w.mode
            INTO target
            FROM workloads w
            JOIN hosts h ON h.id = w.host_id
            JOIN application_profiles ap
                ON ap.id = w.application_profile_id
            JOIN applications a ON a.id = ap.application_id
            WHERE w.id = {workload_id}
              AND h.name = {sql_text(node)}
              AND a.code = 'comfyui'
              AND ap.name IN ('gpu-1', 'gpu-2')
            FOR UPDATE OF w;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'GPU workload not found on this host';
            END IF;

            IF target.mode <> 'gpu' THEN
                RAISE EXCEPTION 'Not a GPU workload';
            END IF;

            IF target.status NOT IN ('stopped', 'failed') THEN
                RAISE EXCEPTION 'Cannot release GPUs from active workload';
            END IF;

            DELETE FROM workload_gpus
            WHERE workload_id = target.id;
        END
        $release$;

        COMMIT;
        """
    )


def workload_set_status(workload_id, status):
    """Record agent-confirmed lifecycle transitions for allowlisted apps."""
    if status not in ('starting', 'running', 'stopping', 'stopped', 'failed'):
        die('invalid workload status')
    # COMFYUI_CPU_WORKLOAD_V1: determine container name from application code.
    # PostgreSQL enforces the profile/application relationship in the JOIN.
    updated = psql(
        f"""
        UPDATE workloads AS w
        SET status = {sql_text(status)},
            container_name = CASE
                WHEN {sql_text(status)} IN ('running', 'starting')
                  THEN CASE
                    WHEN a.code = 'salamander' THEN 'ai-files-' || w.user_id::text
                    WHEN a.code = 'comfyui' THEN 'ai-comfy-' || w.user_id::text
                    ELSE w.container_name
                  END
                ELSE w.container_name
            END,
            started_at = CASE
                WHEN {sql_text(status)} = 'running'
                THEN now() ELSE w.started_at END,
            stopped_at = CASE
                WHEN {sql_text(status)} IN ('stopped', 'failed')
                THEN now() ELSE w.stopped_at END,
            updated_at = now(),
            last_error = CASE
                WHEN {sql_text(status)} = 'failed'
                THEN 'host agent failed to start or stop workload'
                ELSE NULL END
        FROM application_profiles ap
        JOIN applications a ON a.id = ap.application_id
        WHERE w.id = {workload_id}
          AND w.application_profile_id = ap.id
          AND (
              (w.mode = 'utility' AND a.code = 'salamander')
              OR (w.mode = 'preparation' AND a.code = 'comfyui'
                  AND ap.name IN ('cpu-test', 'cpu-prep') AND ap.gpu_min = 0 AND ap.gpu_max = 0)
              OR (w.mode = 'gpu' AND a.code = 'comfyui'
                  AND ap.requires_reservation = true
                  AND (
                      (ap.name = 'gpu-1' AND ap.gpu_min = 1 AND ap.gpu_max = 1)
                      OR
                      (ap.name = 'gpu-2' AND ap.gpu_min = 2 AND ap.gpu_max = 2)
                  ))
          )
        RETURNING w.id;
        """
    )
    if updated.splitlines()[:1] != [str(workload_id)]:
        die(f"cannot update workload {workload_id}")


# WORKLOAD_STATE_SYNC_V1

def workload_list_local():
    """List managed workload IDs for precisely this configured host."""
    cfg = load_config()
    node = cfg.get("node", {}).get("name")
    if not isinstance(node, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", node):
        die("invalid node name")
    result = psql(
        f"""
        SELECT COALESCE(json_agg(w.id ORDER BY w.id), '[]'::json)::text
        FROM workloads w
        JOIN hosts h ON h.id = w.host_id
        JOIN application_profiles ap ON ap.id = w.application_profile_id
        JOIN applications a ON a.id = ap.application_id
        WHERE h.name = {sql_text(node)}
          AND (
            (a.code = 'salamander' AND w.mode = 'utility')
            OR (a.code = 'comfyui' AND w.mode = 'preparation'
                AND ap.name IN ('cpu-test', 'cpu-prep') AND ap.gpu_min = 0 AND ap.gpu_max = 0)
              OR (w.mode = 'gpu' AND a.code = 'comfyui'
                  AND ap.requires_reservation = true
                  AND (
                      (ap.name = 'gpu-1' AND ap.gpu_min = 1 AND ap.gpu_max = 1)
                      OR
                      (ap.name = 'gpu-2' AND ap.gpu_min = 2 AND ap.gpu_max = 2)
                  ))
          );
        """
    )
    print(result or "[]")

def check_mount(path):
    if not path.is_dir():
        die(f"storage root does not exist: {path}")

    if not os.path.ismount(path):
        die(f"storage root is not mounted: {path}")


def resolve_storage(cfg, row):
    source_ref = row["source_ref"]

    if source_ref.startswith("bootstrap:"):
        key = source_ref.split(":", 1)[1]

        try:
            return Path(cfg["paths"][key])
        except KeyError:
            die(f"unknown bootstrap path: {source_ref}")

    if row["mount_point"]:
        return Path(row["mount_point"])

    die(f"cannot resolve storage {row['storage_name']}")


def quota_is_on(path):
    env = os.environ.copy()
    env["LC_ALL"] = "C"

    result = subprocess.run(
        ["quotaon", "-p", "-u", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )

    output = result.stdout.lower()
    return " is on" in output


def bytes_to_kib(value):
    if value is None or int(value) == 0:
        return 0

    value = int(value)
    return (value + 1023) // 1024


def format_bytes(value):
    if value is None:
        return "unset"

    value = int(value)

    if value == 0:
        return "unlimited"

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}"
        value /= 1024

    return str(value)


def provision_user(user_id, finalize=True):
    cfg = load_config()

    data_root = Path(cfg["paths"]["data_root"])
    models_root = Path(cfg["paths"]["models_root"])

    check_mount(data_root)
    check_mount(models_root)

    user = get_user(user_id)

    if not user["enabled"]:
        die(f"user {user['username']} is disabled")

    username = user["username"]

    if not USERNAME_RE.fullmatch(username):
        die(f"invalid username: {username!r}")

    uid = int(user["uid"])
    gid = int(user["gid"])

    if uid < 1000 or gid < 1000:
        die("refusing UID/GID below 1000")

    data_dir = data_root / "users" / username
    models_dir = models_root / "users" / username

    psql(
        f"""
        UPDATE users
        SET provisioning_status = 'provisioning',
            provisioning_error = NULL,
            updated_at = now()
        WHERE id = {user_id};
        """
    )

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        models_dir.mkdir(parents=True, exist_ok=True)

        os.chown(data_dir, uid, gid)
        os.chown(models_dir, uid, gid)

        os.chmod(data_dir, 0o700)
        os.chmod(models_dir, 0o750)

        if finalize:
            psql(
                f"""
                UPDATE users
                SET provisioning_status = 'ready',
                    provisioned_at = now(),
                    provisioning_error = NULL,
                    updated_at = now()
                WHERE id = {user_id};
                """
            )

    except Exception as exc:
        try:
            psql(
                f"""
                UPDATE users
                SET provisioning_status = 'failed',
                    provisioning_error = {sql_text(str(exc))},
                    updated_at = now()
                WHERE id = {user_id};
                """
            )
        finally:
            raise

    print(f"user:       {username}")
    print(f"uid:        {uid}")
    print(f"gid:        {gid}")
    print(f"data:       {data_dir}")
    print(f"models:     {models_dir}")
    print(
        "status:     "
        + ("ready" if finalize else "provisioning")
    )


def quota_apply(user_id):
    cfg = load_config()
    user = get_user(user_id)

    if not user["enabled"]:
        die(f"user {user['username']} is disabled")

    uid = int(user["uid"])
    rows = get_quotas(user_id)

    if not rows:
        die(f"no quota configuration for user {user['username']}")

    for row in rows:
        path = resolve_storage(cfg, row)
        check_mount(path)

        if not quota_is_on(path):
            die(f"user quota is not active on {path}")

        soft_kib = bytes_to_kib(row["soft_bytes"])
        hard_kib = bytes_to_kib(row["hard_bytes"])

        result = subprocess.run(
            [
                "setquota",
                "-u",
                str(uid),
                str(soft_kib),
                str(hard_kib),
                "0",
                "0",
                str(path),
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode != 0:
            error = result.stderr.strip() or "setquota failed"

            psql(
                f"""
                UPDATE filesystem_quotas
                SET last_checked_at = now(),
                    last_error = {sql_text(error)}
                WHERE id = {int(row['quota_id'])};
                """
            )

            die(f"{row['storage_name']}: {error}")

        effective_soft = soft_kib * 1024 if soft_kib else 0
        effective_hard = hard_kib * 1024 if hard_kib else 0

        psql(
            f"""
            UPDATE filesystem_quotas
            SET effective_soft_bytes = {sql_num(effective_soft)},
                effective_hard_bytes = {sql_num(effective_hard)},
                last_applied_at = now(),
                last_checked_at = now(),
                last_error = NULL
            WHERE id = {int(row['quota_id'])};
            """
        )

        print(
            f"{row['storage_name']}: "
            f"soft={format_bytes(effective_soft)}, "
            f"hard={format_bytes(effective_hard)}, "
            f"mount={path}"
        )



def provision_user_full(user_id):
    """Provision directories and quotas as one workflow."""

    try:
        provision_user(user_id, finalize=False)
        quota_apply(user_id)

    except (Exception, SystemExit) as exc:
        message = (
            str(exc)
            if isinstance(exc, Exception)
            else "Storage provisioning command failed"
        )

        psql(
            f"""
            UPDATE users
            SET provisioning_status = 'failed',
                provisioning_error = {sql_text(message[:1500])},
                updated_at = now()
            WHERE id = {user_id};
            """
        )
        raise

    # Storage is ready, but the full user provisioning
    # is not complete until the connector and launcher work.
    print("status:     storage-ready; provisioning continues")

def quota_status(user_id):
    cfg = load_config()
    user = get_user(user_id)
    rows = get_quotas(user_id)

    print(
        f"user: {user['username']} "
        f"(uid={user['uid']}, gid={user['gid']})"
    )

    for row in rows:
        path = resolve_storage(cfg, row)

        print()
        print(f"storage:     {row['storage_name']}")
        print(f"mount:       {path}")
        print(f"quota-on:    {'yes' if quota_is_on(path) else 'NO'}")
        print(f"soft DB:     {format_bytes(row['soft_bytes'])}")
        print(f"hard DB:     {format_bytes(row['hard_bytes'])}")
        print(
            f"soft applied:{format_bytes(row['effective_soft_bytes']):>12}"
        )
        print(
            f"hard applied:{format_bytes(row['effective_hard_bytes']):>12}"
        )

    print()
    subprocess.run(
        ["quota", "-u", "-s", str(user["uid"])],
        check=False,
    )


def main():
    if os.geteuid() != 0:
        die("must be run as root")

    if len(sys.argv) == 2 and sys.argv[1] == "workload-list-local":
        workload_list_local()
        return

    if len(sys.argv) != 3:
        die(
            "usage: ai-platform-helper "
            "<provision-user|quota-apply|quota-status|workload-describe|"
            "workload-set-*> <user-id-or-workload-id>"
        )

    command = sys.argv[1]

    try:
        user_id = int(sys.argv[2])
    except ValueError:
        die("user-id must be numeric")

    if user_id <= 0:
        die("user-id must be greater than zero")

    if command == "workload-describe":
        workload_describe(user_id)
    elif command == "workload-allocate-gpus":
        workload_allocate_gpus(user_id)
    elif command == "workload-release-gpus":
        workload_release_gpus(user_id)
    elif command == "workload-set-starting":
        workload_set_status(user_id, "starting")
    elif command == "workload-set-running":
        workload_set_status(user_id, "running")
    elif command == "workload-set-stopping":
        workload_set_status(user_id, "stopping")
    elif command == "workload-set-stopped":
        workload_set_status(user_id, "stopped")
    elif command == "workload-set-failed":
        workload_set_status(user_id, "failed")
    elif command == "provision-user":
        provision_user(user_id)
    elif command == "provision-user-full":
        provision_user_full(user_id)
    elif command == "quota-apply":
        quota_apply(user_id)
    elif command == "quota-status":
        quota_status(user_id)
    else:
        die(f"unknown command: {command}")


if __name__ == "__main__":
    main()
