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


def provision_user(user_id):
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
    print("status:     ready")


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

    if len(sys.argv) != 3:
        die(
            "usage: ai-platform-helper "
            "<provision-user|quota-apply|quota-status> <user-id>"
        )

    command = sys.argv[1]

    try:
        user_id = int(sys.argv[2])
    except ValueError:
        die("user-id must be numeric")

    if user_id <= 0:
        die("user-id must be greater than zero")

    if command == "provision-user":
        provision_user(user_id)
    elif command == "quota-apply":
        quota_apply(user_id)
    elif command == "quota-status":
        quota_status(user_id)
    else:
        die(f"unknown command: {command}")


if __name__ == "__main__":
    main()
