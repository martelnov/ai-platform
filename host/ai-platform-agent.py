#!/usr/bin/env python3

import hashlib
import json
import os
import re
import socketserver
import threading
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path


SOCKET_PATH = "/run/ai-platform/agent.sock"
CONFIG_FILE = "/etc/ai-platform/config.toml"

MAX_REQUEST = 65536

TAILSCALE_IMAGE = "tailscale/tailscale:v1.104.1"
WORKLOAD_LOCK = threading.Lock()

NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


def run(
    cmd,
    check=True,
    input_text=None,
    timeout=None,
):
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"command failed: {cmd[0]}"
        )

    return result


def load_config():
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def resolve_user_storage_root(username, tree):
    if (
        not isinstance(username, str)
        or not USERNAME_RE.fullmatch(username)
    ):
        raise ValueError("invalid username")

    if tree not in ("data", "models"):
        raise ValueError("invalid storage tree")

    cfg = load_config()

    root_key = (
        "data_root"
        if tree == "data"
        else "models_root"
    )

    storage_root = Path(
        cfg["paths"][root_key]
    ).resolve()

    users_root = (
        storage_root / "users"
    ).resolve()

    user_root = users_root / username

    if not user_root.is_dir():
        raise ValueError(
            "user storage root does not exist"
        )

    resolved_user_root = user_root.resolve()

    if resolved_user_root != user_root:
        raise ValueError(
            "user storage root must not be a symlink"
        )

    return resolved_user_root


def resolve_user_mount_path(username, tree, mount_path):
    if not isinstance(mount_path, str):
        raise ValueError("invalid mount_path")

    if not mount_path.startswith("/") or mount_path == "/":
        raise ValueError(
            "mount_path must be below the user storage root"
        )

    relative_text = mount_path[1:]
    parts = relative_text.split("/")

    if any(
        not part or part in (".", "..") or "\x00" in part
        for part in parts
    ):
        raise ValueError("invalid mount_path")

    user_root = resolve_user_storage_root(
        username,
        tree,
    )

    target = (
        user_root.joinpath(*parts)
    ).resolve(strict=False)

    if (
        target == user_root
        or user_root not in target.parents
    ):
        raise ValueError(
            "mount_path escapes user storage root"
        )

    return target


def resolve_platform_directory_source(tree, source_ref):
    if tree not in ("data", "models"):
        raise ValueError("invalid storage tree")

    if not isinstance(source_ref, str):
        raise ValueError("invalid source_ref")

    source_path = Path(source_ref)

    if not source_path.is_absolute():
        raise ValueError(
            "directory source_ref must be absolute"
        )

    try:
        source = source_path.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(
            "directory source does not exist"
        )

    if not source.is_dir():
        raise ValueError(
            "directory source is not a directory"
        )

    forbidden_roots = (
        Path("/"),
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path("/run"),
    )

    for forbidden in forbidden_roots:
        if (
            source == forbidden
            or (
                forbidden != Path("/")
                and forbidden in source.parents
            )
        ):
            raise ValueError(
                "directory source is a protected system path"
            )

    return source


def resolve_nfs_source(source_ref):
    if not isinstance(source_ref, str):
        raise ValueError("invalid NFS source_ref")

    if (
        not source_ref
        or any(ch.isspace() for ch in source_ref)
        or "\x00" in source_ref
    ):
        raise ValueError("invalid NFS source_ref")

    if ":" not in source_ref:
        raise ValueError(
            "NFS source must be host:/absolute/path"
        )

    host, export = source_ref.split(":", 1)

    if (
        not host
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}",
            host,
        )
    ):
        raise ValueError("invalid NFS host")

    if (
        not export.startswith("/")
        or export == "/"
        or "//" in export
    ):
        raise ValueError("invalid NFS export path")

    parts = export[1:].split("/")

    if any(
        not part or part in (".", "..")
        for part in parts
    ):
        raise ValueError("invalid NFS export path")

    return f"{host}:{export}"


def resolve_smb_source(source_ref):
    if not isinstance(source_ref, str):
        raise ValueError("invalid SMB source_ref")

    if (
        not source_ref.startswith("//")
        or any(ch.isspace() for ch in source_ref)
        or "\x00" in source_ref
    ):
        raise ValueError("invalid SMB source_ref")

    parts = source_ref[2:].split("/")

    if len(parts) != 2:
        raise ValueError(
            "SMB source must be //host/share"
        )

    host, share = parts

    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}",
        host,
    ):
        raise ValueError("invalid SMB host")

    if (
        not share
        or share in (".", "..")
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._$-]{0,79}",
            share,
        )
    ):
        raise ValueError("invalid SMB share")

    return f"//{host}/{share}"


def normalize_smb_credentials(credentials):
    if not isinstance(credentials, dict):
        raise ValueError("SMB credentials are required")

    username = credentials.get("username")
    password = credentials.get("password")
    domain = credentials.get("domain")

    if (
        not isinstance(username, str)
        or not username
        or len(username) > 255
        or any(ch in username for ch in "\r\n\x00")
    ):
        raise ValueError("invalid SMB username")

    if (
        not isinstance(password, str)
        or len(password) > 4096
        or any(ch in password for ch in "\r\n\x00")
    ):
        raise ValueError("invalid SMB password")

    if domain is not None:
        if (
            not isinstance(domain, str)
            or not domain
            or len(domain) > 255
            or any(ch in domain for ch in "\r\n\x00")
        ):
            raise ValueError("invalid SMB domain")

    return username, password, domain


def write_smb_credentials(credentials):
    username, password, domain = normalize_smb_credentials(
        credentials
    )

    runtime_dir = Path(
        "/run/ai-platform/smb-credentials"
    )

    runtime_dir.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )
    os.chmod(runtime_dir, 0o700)

    fd, filename = tempfile.mkstemp(
        prefix="mount-",
        dir=runtime_dir,
        text=True,
    )

    try:
        os.fchmod(fd, 0o600)

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(f"username={username}\n")
            f.write(f"password={password}\n")

            if domain is not None:
                f.write(f"domain={domain}\n")

        return Path(filename)

    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass

        try:
            os.unlink(filename)
        except OSError:
            pass

        raise


def user_mount_status(request):
    username = request.get("username")
    tree = request.get("tree")
    mount_path = request.get("mount_path")

    target = resolve_user_mount_path(
        username,
        tree,
        mount_path,
    )

    result = run(
        [
            "findmnt",
            "-J",
            "-M",
            str(target),
            "-o",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        ],
        check=False,
    )

    if result.returncode != 0:
        return {
            "ok": True,
            "mounted": False,
            "target": str(target),
        }

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            "invalid findmnt response"
        )

    filesystems = data.get("filesystems") or []

    if len(filesystems) != 1:
        raise RuntimeError(
            "unexpected findmnt response"
        )

    filesystem = filesystems[0]

    return {
        "ok": True,
        "mounted": True,
        "target": str(target),
        "source": filesystem.get("source"),
        "filesystem_type": filesystem.get("fstype"),
        "options": filesystem.get("options"),
    }


def user_mount_nested_targets(target):
    result = run(
        [
            "findmnt",
            "-R",
            "-J",
            "-M",
            str(target),
            "-o",
            "TARGET",
        ],
        check=False,
    )

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            "invalid recursive findmnt response"
        )

    nested = []

    def visit(filesystems):
        for filesystem in filesystems or []:
            target_text = filesystem.get("target")

            if target_text:
                current = Path(target_text).resolve(
                    strict=False
                )

                if (
                    current != target
                    and target in current.parents
                ):
                    nested.append(str(current))

            visit(filesystem.get("children"))

    visit(data.get("filesystems"))

    return nested


def user_encryption_status(request):
    username = request.get("username")
    tree = request.get("tree")
    root_path = request.get("root_path")

    target = resolve_user_mount_path(
        username,
        tree,
        root_path,
    )

    if not target.exists():
        return {
            "ok": True,
            "state": "not_initialized",
            "target": str(target),
        }

    if not target.is_dir():
        raise RuntimeError(
            "encryption root is not a directory"
        )

    result = run(
        [
            "fscrypt",
            "status",
            str(target),
        ],
        check=False,
    )

    if result.returncode != 0:
        error_text = (
            result.stderr.strip()
            or result.stdout.strip()
        )

        if (
            "is not encrypted" in error_text
            or "not setup for encryption with fscrypt"
            in error_text
        ):
            return {
                "ok": True,
                "state": "not_initialized",
                "target": str(target),
            }

        raise RuntimeError(
            error_text
            or "unable to determine encryption status"
        )

    output = result.stdout

    if "Unlocked: Yes" in output:
        state = "unlocked"
    elif "Unlocked: No" in output:
        state = "locked"
    else:
        raise RuntimeError(
            "unable to determine fscrypt lock state"
        )

    policy = None

    for line in output.splitlines():
        if line.startswith("Policy:"):
            policy = line.split(":", 1)[1].strip()
            break

    return {
        "ok": True,
        "state": state,
        "target": str(target),
        "policy": policy,
        "encryption_type": "fscrypt",
    }


def user_encryption_protector(request):
    status = user_encryption_status(request)

    if status["state"] == "not_initialized":
        raise RuntimeError(
            "encryption is not initialized"
        )

    target = status["target"]

    result = run([
        "fscrypt",
        "status",
        target,
    ])

    protector_ids = []

    for line in result.stdout.splitlines():
        match = re.match(
            r"^([0-9a-fA-F]{16})\s+",
            line.strip(),
        )

        if match:
            protector_ids.append(
                match.group(1).lower()
            )

    if len(protector_ids) != 1:
        raise RuntimeError(
            "expected exactly one fscrypt protector"
        )

    mount_result = run([
        "findmnt",
        "-n",
        "-o",
        "TARGET",
        "--target",
        target,
    ])

    mountpoint = mount_result.stdout.strip()

    if not mountpoint.startswith("/"):
        raise RuntimeError(
            "unable to determine filesystem mountpoint"
        )

    return {
        "target": target,
        "mountpoint": mountpoint,
        "protector": protector_ids[0],
    }


def prepare_user_encryption_directory(
    username,
    tree,
    root_path,
):
    user_root = resolve_user_storage_root(
        username,
        tree,
    )

    target = resolve_user_mount_path(
        username,
        tree,
        root_path,
    )

    root_stat = user_root.stat()
    uid = root_stat.st_uid
    gid = root_stat.st_gid
    mode = root_stat.st_mode & 0o777

    parts = root_path[1:].split("/")
    current = user_root
    created = []

    for part in parts:
        current = current / part

        if current.is_symlink():
            raise RuntimeError(
                "encryption path must not contain symlinks"
            )

        if current.exists():
            if not current.is_dir():
                raise RuntimeError(
                    "encryption path component is not a directory"
                )

            current_stat = current.stat()

            if (
                current_stat.st_uid != uid
                or current_stat.st_gid != gid
            ):
                raise RuntimeError(
                    "existing encryption path has unexpected ownership"
                )

        else:
            current.mkdir(mode=mode)
            os.chown(current, uid, gid)
            os.chmod(current, mode)
            created.append(current)

    if current.resolve() != target:
        raise RuntimeError(
            "encryption path resolution changed unexpectedly"
        )

    return target, created


def validate_encryption_passphrase(passphrase):
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError(
            "encryption passphrase is required"
        )

    if len(passphrase) > 4096:
        raise ValueError(
            "encryption passphrase is too long"
        )

    if any(
        char in passphrase
        for char in ("\x00", "\n", "\r")
    ):
        raise ValueError(
            "encryption passphrase contains unsupported characters"
        )


def user_encryption_enable(request):
    username = request.get("username")
    tree = request.get("tree")
    root_path = request.get("root_path")
    passphrase = request.get("passphrase")

    validate_encryption_passphrase(passphrase)

    status = user_encryption_status(request)

    if status["state"] != "not_initialized":
        status["changed"] = False
        return status

    target = None
    created = []

    try:
        target, created = prepare_user_encryption_directory(
            username,
            tree,
            root_path,
        )

        if any(target.iterdir()):
            raise RuntimeError(
                "encryption directory must be empty"
            )

        digest = hashlib.sha256(
            (
                f"{username}\0"
                f"{tree}\0"
                f"{root_path}"
            ).encode("utf-8")
        ).hexdigest()[:16]

        protector_name = (
            "ai-platform-"
            + username[:20]
            + "-"
            + tree
            + "-"
            + digest
        )

        run(
            [
                "fscrypt",
                "encrypt",
                str(target),
                "--source=custom_passphrase",
                f"--name={protector_name}",
                "--no-recovery",
            ],
            input_text=(
                passphrase
                + "\n"
                + passphrase
                + "\n"
            ),
        )

        status = user_encryption_status(request)

        if status["state"] != "unlocked":
            raise RuntimeError(
                "encryption initialization verification failed"
            )

        status["changed"] = True
        return status

    except Exception:
        try:
            status = user_encryption_status(request)
        except Exception:
            status = None

        if (
            status
            and status.get("state") == "not_initialized"
        ):
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass

        raise


def user_encryption_change_passphrase(request):
    old_passphrase = request.get("old_passphrase")
    new_passphrase = request.get("new_passphrase")

    validate_encryption_passphrase(old_passphrase)
    validate_encryption_passphrase(new_passphrase)

    metadata = user_encryption_protector(request)

    try:
        run(
            [
                "fscrypt",
                "metadata",
                "change-passphrase",
                (
                    "--protector="
                    + metadata["mountpoint"]
                    + ":"
                    + metadata["protector"]
                ),
            ],
            input_text=(
                old_passphrase
                + "\n"
                + new_passphrase
                + "\n"
                + new_passphrase
                + "\n"
            ),
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "passphrase change failed: "
            "invalid old passphrase or fscrypt timed out"
        ) from None

    status = user_encryption_status(request)
    status["changed"] = True
    return status


def user_encryption_lock(request):
    status = user_encryption_status(request)

    if status["state"] == "not_initialized":
        raise RuntimeError(
            "encryption is not initialized"
        )

    if status["state"] == "locked":
        status["changed"] = False
        return status

    target = status["target"]

    run([
        "fscrypt",
        "lock",
        target,
        "--all-users",
    ])

    status = user_encryption_status(request)

    if status["state"] != "locked":
        raise RuntimeError(
            "encryption lock verification failed"
        )

    status["changed"] = True
    return status


def user_encryption_unlock(request):
    passphrase = request.get("passphrase")

    validate_encryption_passphrase(passphrase)

    status = user_encryption_status(request)

    if status["state"] == "not_initialized":
        raise RuntimeError(
            "encryption is not initialized"
        )

    if status["state"] == "unlocked":
        status["changed"] = False
        return status

    target = status["target"]

    try:
        run(
            [
                "fscrypt",
                "unlock",
                target,
            ],
            input_text=passphrase + "\n",
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "encryption unlock failed: "
            "invalid passphrase or fscrypt timed out"
        ) from None

    status = user_encryption_status(request)

    if status["state"] != "unlocked":
        raise RuntimeError(
            "encryption unlock verification failed"
        )

    status["changed"] = True
    return status


def resolve_user_mount_source(tree, source_type, source_ref):
    if source_type == "directory":
        return resolve_platform_directory_source(
            tree,
            source_ref,
        )

    if source_type == "nfs":
        return resolve_nfs_source(source_ref)

    if source_type == "smb":
        return resolve_smb_source(source_ref)

    raise ValueError(
        "unsupported mount source_type"
    )


def user_mount_source_matches(
    status,
    source_type,
    source,
):
    if not status.get("mounted"):
        return False

    if source_type == "directory":
        try:
            return os.path.samefile(
                source,
                status["target"],
            )
        except OSError:
            return False

    if source_type == "nfs":
        return (
            status.get("filesystem_type")
            in ("nfs", "nfs4")
            and status.get("source") == str(source)
        )

    if source_type == "smb":
        return (
            status.get("filesystem_type") == "cifs"
            and status.get("source") == str(source)
        )

    return False


def mount_smb_source(
    username,
    tree,
    source,
    target,
    credentials,
):
    user_root = resolve_user_storage_root(
        username,
        tree,
    )

    st = user_root.stat()
    uid = st.st_uid
    gid = st.st_gid

    if uid < 1000 or gid < 1000:
        raise ValueError(
            "refusing SMB mount for UID/GID below 1000"
        )

    credentials_path = write_smb_credentials(
        credentials
    )

    try:
        options = ",".join([
            f"credentials={credentials_path}",
            f"uid={uid}",
            f"gid={gid}",
            "forceuid",
            "forcegid",
            "nosuid",
            "nodev",
        ])

        run([
            "mount",
            "-t",
            "cifs",
            "-o",
            options,
            str(source),
            str(target),
        ])

    finally:
        try:
            credentials_path.unlink()
        except FileNotFoundError:
            pass


def user_mount_apply(request):
    username = request.get("username")
    tree = request.get("tree")
    mount_path = request.get("mount_path")
    scope = request.get("scope")
    source_type = request.get("source_type")
    source_ref = request.get("source_ref")

    if scope != "platform":
        raise ValueError(
            "only platform mounts are supported"
        )

    target = resolve_user_mount_path(
        username,
        tree,
        mount_path,
    )

    source = resolve_user_mount_source(
        tree,
        source_type,
        source_ref,
    )

    if source_type == "directory":
        if (
            source == target
            or source in target.parents
            or target in source.parents
        ):
            raise ValueError(
                "directory source and mount target must not contain each other"
            )

    status = user_mount_status(request)

    if status["mounted"]:
        if not user_mount_source_matches(
            status,
            source_type,
            source,
        ):
            raise RuntimeError(
                "target is already mounted from another source"
            )

        status["changed"] = False
        status["source_ref"] = str(source)
        return status

    if target.exists():
        if not target.is_dir():
            raise RuntimeError(
                "mount target is not a directory"
            )

        if any(target.iterdir()):
            raise RuntimeError(
                "refusing to hide non-empty mount target"
            )
    else:
        target.mkdir(
            parents=True,
            mode=0o750,
        )

    os.chmod(target, 0o750)

    if source_type == "directory":
        run([
            "mount",
            "--bind",
            str(source),
            str(target),
        ])

    elif source_type == "nfs":
        run([
            "mount",
            "-t",
            "nfs",
            "-o",
            "nosuid,nodev",
            str(source),
            str(target),
        ])

    elif source_type == "smb":
        mount_smb_source(
            username,
            tree,
            source,
            target,
            request.get("credentials"),
        )

    else:
        raise ValueError(
            "unsupported mount source_type"
        )

    status = user_mount_status(request)

    if not user_mount_source_matches(
        status,
        source_type,
        source,
    ):
        run(
            ["umount", str(target)],
            check=False,
        )
        raise RuntimeError(
            "mount verification failed"
        )

    status["changed"] = True
    status["source_ref"] = str(source)

    return status


def user_mount_remove(request):
    username = request.get("username")
    tree = request.get("tree")
    mount_path = request.get("mount_path")
    scope = request.get("scope")
    source_type = request.get("source_type")
    source_ref = request.get("source_ref")

    if scope != "platform":
        raise ValueError(
            "only platform mounts are supported"
        )

    target = resolve_user_mount_path(
        username,
        tree,
        mount_path,
    )

    source = resolve_user_mount_source(
        tree,
        source_type,
        source_ref,
    )

    status = user_mount_status(request)

    if not status["mounted"]:
        status["changed"] = False
        status["source_ref"] = str(source)
        return status

    if not user_mount_source_matches(
        status,
        source_type,
        source,
    ):
        raise RuntimeError(
            "refusing to unmount unexpected source"
        )

    nested_targets = user_mount_nested_targets(
        target
    )

    if nested_targets:
        raise RuntimeError(
            "refusing to unmount target with nested mounts: "
            + ", ".join(nested_targets)
        )

    run([
        "umount",
        str(target),
    ])

    status = user_mount_status(request)

    if status["mounted"]:
        raise RuntimeError(
            "unmount verification failed"
        )

    try:
        target.rmdir()
    except OSError:
        pass

    status["changed"] = True
    status["source_ref"] = str(source)

    return status


def container_exists(name):
    result = run(
        ["docker", "inspect", name],
        check=False,
    )

    return result.returncode == 0


def container_running(name):
    result = run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            name,
        ],
        check=False,
    )

    return (
        result.returncode == 0
        and result.stdout.strip() == "true"
    )


def remove_container(name):
    if container_exists(name):
        run(
            ["docker", "rm", "-f", name],
            check=False,
        )


def tailscale_status(name):
    result = run(
        [
            "docker",
            "exec",
            name,
            "tailscale",
            "status",
            "--json",
        ],
        check=False,
    )

    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def wait_for_tailscale(name, timeout=90):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        status = tailscale_status(name)

        if status and status.get("BackendState") == "Running":
            addresses = status.get("TailscaleIPs", [])

            ipv4 = next(
                (
                    value
                    for value in addresses
                    if "." in value
                ),
                None,
            )

            self_info = status.get("Self", {})

            return {
                "status": status,
                "address": ipv4,
                "provider_id": self_info.get("ID"),
                "dns_name": self_info.get("DNSName"),
            }

        time.sleep(1)

    raise RuntimeError(
        "Tailscale connector did not become ready"
    )


def resolve_state_path(state_path):
    cfg = load_config()

    data_root = Path(
        cfg["paths"]["data_root"]
    ).resolve()

    relative = Path(state_path)

    if relative.is_absolute():
        raise ValueError(
            "state_path must be relative to data_root"
        )

    if ".." in relative.parts:
        raise ValueError(
            "state_path must not contain '..'"
        )

    if relative.parts[:2] != (
        "connectors",
        "tailscale",
    ):
        raise ValueError(
            "state_path must be below connectors/tailscale"
        )

    full = (data_root / relative).resolve()

    allowed_root = (
        data_root / "connectors" / "tailscale"
    ).resolve()

    if full != allowed_root and allowed_root not in full.parents:
        raise ValueError(
            "state_path escapes tailscale connector root"
        )

    full.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    os.chmod(full, 0o700)

    return full


def permanent_container_args(
    container_name,
    node_name,
    state_dir,
):
    return [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--hostname",
        node_name,
        "--restart",
        "unless-stopped",
        "-e",
        "TS_AUTH_ONCE=true",
        "-e",
        "TS_STATE_DIR=/var/lib/tailscale",
        "-e",
        "TS_USERSPACE=false",
        "-e",
        "TS_BOOT_TIMEOUT=90s",
        "--device",
        "/dev/net/tun:/dev/net/tun",
        "--cap-add",
        "NET_ADMIN",
        "--cap-add",
        "NET_RAW",
        "--mount",
        (
            "type=bind,"
            f"src={state_dir},"
            "dst=/var/lib/tailscale"
        ),
        TAILSCALE_IMAGE,
    ]


def provision_tailscale_connector(request):
    connector_id = request.get("connector_id")
    node_name = request.get("node_name")
    state_path = request.get("state_path")
    auth_key = request.get("auth_key")

    if not isinstance(connector_id, int) or connector_id <= 0:
        raise ValueError("invalid connector_id")

    if (
        not isinstance(node_name, str)
        or not NODE_NAME_RE.fullmatch(node_name)
    ):
        raise ValueError("invalid node_name")

    if not isinstance(state_path, str):
        raise ValueError("invalid state_path")

    if (
        not isinstance(auth_key, str)
        or not auth_key.startswith("tskey-auth-")
        or len(auth_key) > 1024
    ):
        raise ValueError("invalid Tailscale auth key")

    state_dir = resolve_state_path(state_path)

    container_name = f"ai-ts-{connector_id}"

    if container_running(container_name):
        current = tailscale_status(container_name)

        if (
            current
            and current.get("BackendState") == "Running"
        ):
            addresses = current.get(
                "TailscaleIPs",
                [],
            )

            ipv4 = next(
                (
                    value
                    for value in addresses
                    if "." in value
                ),
                None,
            )

            self_info = current.get("Self", {})

            return {
                "ok": True,
                "status": "running",
                "container": container_name,
                "address": ipv4,
                "provider_id": self_info.get("ID"),
                "dns_name": self_info.get("DNSName"),
                "existing": True,
            }

    remove_container(container_name)

    image_check = run(
        [
            "docker",
            "image",
            "inspect",
            TAILSCALE_IMAGE,
        ],
        check=False,
    )

    if image_check.returncode != 0:
        run(
            [
                "docker",
                "pull",
                TAILSCALE_IMAGE,
            ]
        )

    runtime_dir = Path(
        "/run/ai-platform/provision"
    )

    runtime_dir.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    os.chmod(runtime_dir, 0o700)

    secret_path = None

    try:
        fd, secret_name = tempfile.mkstemp(
            prefix=f"tailscale-{connector_id}-",
            dir=runtime_dir,
        )

        secret_path = Path(secret_name)

        os.fchmod(fd, 0o600)

        with os.fdopen(fd, "w") as f:
            f.write(f"TS_AUTHKEY={auth_key}\n")

        provision_args = permanent_container_args(
            container_name,
            node_name,
            state_dir,
        )

        # The provisioning credential exists only in /run.
        # Docker reads it for the first authentication container.
        provision_args[-1:-1] = [
            "--env-file",
            str(secret_path),
        ]

        run(provision_args)

        wait_for_tailscale(
            container_name,
            timeout=90,
        )

        # Authentication is now stored in TS_STATE_DIR.
        # Recreate the final connector without auth key.
        remove_container(container_name)

        secret_path.unlink(missing_ok=True)
        secret_path = None

        run(
            permanent_container_args(
                container_name,
                node_name,
                state_dir,
            )
        )

        ready = wait_for_tailscale(
            container_name,
            timeout=90,
        )

        return {
            "ok": True,
            "status": "running",
            "container": container_name,
            "address": ready["address"],
            "provider_id": ready["provider_id"],
            "dns_name": ready["dns_name"],
            "existing": False,
        }

    except Exception:
        remove_container(container_name)
        raise

    finally:
        if secret_path is not None:
            secret_path.unlink(missing_ok=True)



def linux_identity_available(request):
    """Check host UID/GID availability using NSS."""

    import pwd
    import grp

    uid = request.get("uid")
    gid = request.get("gid")

    if type(uid) is not int or type(gid) is not int:
        raise ValueError("UID/GID must be integers")

    if not (20000 <= uid <= 59999):
        raise ValueError("UID outside platform range")

    if not (20000 <= gid <= 59999):
        raise ValueError("GID outside platform range")

    try:
        pwd.getpwuid(uid)
        uid_used = True
    except KeyError:
        uid_used = False

    try:
        grp.getgrgid(gid)
        gid_used = True
    except KeyError:
        gid_used = False

    return {
        "ok": True,
        "available": not uid_used and not gid_used,
        "uid_used": uid_used,
        "gid_used": gid_used,
    }

# Workload runtime: the DB specification is read by the existing host
# helper. No image, path or Docker options are accepted from the request.
WORKLOAD_HELPER = "/usr/local/sbin/ai-platform-helper"


def helper_workload_command(command, workload_id):
    if type(workload_id) is not int or workload_id <= 0:
        raise ValueError("invalid workload_id")

    return run(
        [WORKLOAD_HELPER, command, str(workload_id)],
        timeout=30,
    ).stdout.strip()


def load_workload(workload_id):
    spec = json.loads(
        helper_workload_command("workload-describe", workload_id)
    )

    if not isinstance(spec, dict) or spec.get("id") != workload_id:
        raise RuntimeError("unexpected workload specification")

    return spec


def positive_int(value, name, minimum=1, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def validated_salamander_spec(spec, *, start=False):
    """Initially support utility Salamander only; fail closed otherwise."""
    if spec.get("application_code") != "salamander":
        raise ValueError("only SalamanderRS is supported in this stage")
    if spec.get("mode") != "utility":
        raise ValueError("SalamanderRS requires utility mode")
    if spec.get("requires_reservation") is not False:
        raise ValueError("SalamanderRS must not require GPU reservation")
    if spec.get("gpu_min") != 0 or spec.get("gpu_max") != 0:
        raise ValueError("GPU must be disabled for SalamanderRS")
    if spec.get("command") is not None:
        raise ValueError("custom command not supported for SalamanderRS")

    if spec.get("host_name") != load_config()["node"]["name"]:
        raise ValueError("workload is assigned to another host")

    user_id = positive_int(spec.get("user_id"), "user_id")
    connector_id = positive_int(spec.get("connector_id"), "connector_id")
    uid = positive_int(spec.get("uid"), "uid", 20000, 59999)
    gid = positive_int(spec.get("gid"), "gid", 20000, 59999)
    username = spec.get("username")

    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ValueError("invalid username")

    if start:
        if not all(spec.get(key) is True for key in (
            "user_enabled", "connector_enabled", "application_enabled",
            "profile_enabled",
        )):
            raise RuntimeError("user, connector or application is disabled")
        if spec.get("provisioning_status") != "ready":
            raise RuntimeError("user provisioning not ready")
        if spec.get("connector_status") != "running":
            raise RuntimeError("connector not marked running")

    image = spec.get("image")
    if not isinstance(image, str) or not re.fullmatch(
        r"ai-platform-salamander:[A-Za-z0-9_.-]+", image
    ):
        raise ValueError("unexpected SalamanderRS image")

    return user_id, connector_id, uid, gid, username


def inspect_container(name):
    result = run(["docker", "inspect", name], check=False)
    if result.returncode != 0:
        error = result.stderr.strip()
        if "no such object:" in error.lower() or "no such container:" in error.lower():
            return None
        raise RuntimeError(error or "Docker inspect failed")
    try:
        items = json.loads(result.stdout)
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError("unexpected Docker inspect data")
        return items[0]
    except (ValueError, IndexError, TypeError):
        raise RuntimeError("invalid Docker inspection result")


def salamander_container_name(spec):
    return f"ai-files-{spec['user_id']}"


def validate_salamander_container(info, spec):
    labels = info.get("Config", {}).get("Labels") or {}
    expected = {
        "ai.platform.component": "file-manager",
        "ai.platform.application": "salamander",
        "ai.platform.user_id": str(spec["user_id"]),
        "ai.platform.connector_id": str(spec["connector_id"]),
        "ai.platform.workload_id": str(spec["id"]),
    }
    if any(labels.get(k) != v for k, v in expected.items()):
        raise RuntimeError("refusing to manage a container with different ownership")


def workload_storage_preflight(spec):
    user_id, connector_id, uid, gid, username = validated_salamander_spec(
        spec, start=True
    )

    roots = {}
    cfg = load_config()
    for tree in ("data", "models"):
        root_key = "data_root" if tree == "data" else "models_root"
        if not os.path.ismount(cfg["paths"][root_key]):
            raise RuntimeError(f"{tree} filesystem is not mounted")
        root = resolve_user_storage_root(username, tree)
        st = root.stat()
        if st.st_uid != uid or st.st_gid != gid:
            raise RuntimeError(f"{tree} root has unexpected UID/GID")
        roots[tree] = root

    # Mounts must be prepared in advance through the existing mount API.
    # Never start with a missing mount and accidentally expose its underlying
    # empty directory in place of the intended filesystem.
    for mount in spec.get("mounts", []):
        if mount.get("scope") != "platform":
            raise RuntimeError("user-managed mount not yet supported")
        status = user_mount_status({
            "username": username,
            "tree": mount["tree"],
            "mount_path": mount["mount_path"],
        })
        if not status["mounted"]:
            raise RuntimeError(f"mount not ready: {mount['tree']}:{mount['mount_path']}")
        source = resolve_user_mount_source(
            mount["tree"], mount["source_type"], mount["source_ref"]
        )
        if not user_mount_source_matches(status, mount["source_type"], source):
            raise RuntimeError(f"mount source mismatch: {mount['mount_path']}")

    # Until lifecycle-aware locking is implemented, refuse to start while
    # any configured fscrypt root is locked. Existing unlock API handles it.
    for er in spec.get("encryption_roots", []):
        status = user_encryption_status({
            "username": username,
            "tree": er["tree"],
            "root_path": er["root_path"],
        })
        if status["state"] == "locked":
            raise RuntimeError(f"fscrypt locked: {er['tree']}:{er['root_path']}")

    return roots


def salamander_docker_args(spec, roots, connector_id, uid, gid):
    env = spec.get("environment")
    expected_env = {
        "SALAMANDER_DATA_ROOT": "/data",
        "SALAMANDER_MODELS_ROOT": "/models",
        "SALAMANDER_ENABLE_WRITES": "1",
        "SALAMANDER_PORT": "8090",
    }
    if env != expected_env:
        raise ValueError("unexpected SalamanderRS environment in DB")

    app_config = spec.get("application_config") or {}
    if app_config.get("include_common") is not False:
        raise ValueError("COMMON models must not be shared with SalamanderRS")

    config = spec.get("profile_config") or {}
    if config.get("cpu_pool") != "interactive":
        raise ValueError("unexpected CPU pool")
    pids = positive_int(config.get("pids_limit"), "pids_limit", 16, 4096)
    memory = positive_int(spec.get("memory_limit_bytes"), "memory", 64 * 1024**2)
    swap = positive_int(config.get("memory_swap_bytes"), "memory_swap_bytes")
    if swap != memory:
        raise ValueError("this utility profile must disable memory swap")
    cpu = spec.get("cpu_limit")
    if type(cpu) not in (int, float) or not (0 < cpu <= 64):
        raise ValueError("invalid cpu_limit")

    args = [
        "docker", "run", "-d",
        "--name", salamander_container_name(spec),
        "--network", f"container:ai-ts-{connector_id}",
        "--restart", "no",
        "--user", f"{uid}:{gid}",
        "--cpus", str(cpu),
        "--memory", str(memory),
        "--memory-swap", str(swap),
        "--pids-limit", str(pids),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    for key, value in (
        ("ai.platform.component", "file-manager"),
        ("ai.platform.application", "salamander"),
        ("ai.platform.user_id", str(spec["user_id"])),
        ("ai.platform.connector_id", str(connector_id)),
        ("ai.platform.workload_id", str(spec["id"])),
    ):
        args.extend(["--label", f"{key}={value}"])
    for tree in ("data", "models"):
        args.extend([
            "--mount",
            f"type=bind,src={roots[tree]},dst=/{tree}",
        ])
    for key, value in sorted(expected_env.items()):
        args.extend(["-e", f"{key}={value}"])
    args.append(spec["image"])
    return args


def workload_start(request):
    workload_id = positive_int(request.get("workload_id"), "workload_id")
    with WORKLOAD_LOCK:
        spec = load_workload(workload_id)
        user_id, connector_id, uid, gid, username = validated_salamander_spec(
            spec, start=True
        )
        roots = workload_storage_preflight(spec)
        ts_info = inspect_container(f"ai-ts-{connector_id}")
        if ts_info is None or not ts_info["State"]["Running"]:
            raise RuntimeError("Tailscale connector is not running")
        ts_state = tailscale_status(f"ai-ts-{connector_id}")
        if not ts_state or ts_state.get("BackendState") != "Running":
            raise RuntimeError("Tailscale connector is not ready")

        name = salamander_container_name(spec)
        # Check availability before retiring any old, stopped instance.
        run(["docker", "image", "inspect", spec["image"]])
        current = inspect_container(name)
        if current is not None:
            validate_salamander_container(current, spec)
            if current["State"]["Running"]:
                expected_mode = "container:" + ts_info["Id"]
                if current["HostConfig"]["NetworkMode"] != expected_mode:
                    raise RuntimeError("existing container is in a different network")
                if (current["Config"].get("Image") != spec["image"]
                    or current["Config"].get("User") != f"{uid}:{gid}"):
                    raise RuntimeError("existing container has unexpected image or UID/GID")
                helper_workload_command("workload-set-running", workload_id)
                return {"ok": True, "status": "running", "container": name,
                        "existing": True}
            run(["docker", "rm", name])

        args = salamander_docker_args(spec, roots, connector_id, uid, gid)
        helper_workload_command("workload-set-starting", workload_id)
        try:
            run(args, timeout=90)
            if not container_running(name):
                raise RuntimeError("SalamanderRS exited immediately after start")
            helper_workload_command("workload-set-running", workload_id)
        except Exception:
            info = inspect_container(name)
            if info is not None:
                validate_salamander_container(info, spec)
                run(["docker", "rm", "-f", name], check=False)
            helper_workload_command("workload-set-failed", workload_id)
            raise
        return {"ok": True, "status": "running", "container": name,
                "existing": False}


def workload_status(request):
    workload_id = positive_int(request.get("workload_id"), "workload_id")
    spec = load_workload(workload_id)
    validated_salamander_spec(spec)
    name = salamander_container_name(spec)
    info = inspect_container(name)
    if info is None:
        return {"ok": True, "status": "stopped", "container": name,
                "database_status": spec["status"]}
    validate_salamander_container(info, spec)
    return {"ok": True,
            "status": "running" if info["State"]["Running"] else "stopped",
            "container": name, "database_status": spec["status"]}


def workload_stop(request):
    workload_id = positive_int(request.get("workload_id"), "workload_id")
    with WORKLOAD_LOCK:
        spec = load_workload(workload_id)
        validated_salamander_spec(spec)
        name = salamander_container_name(spec)
        info = inspect_container(name)
        if info is None:
            helper_workload_command("workload-set-stopped", workload_id)
            return {"ok": True, "status": "stopped", "existing": False}
        validate_salamander_container(info, spec)
        helper_workload_command("workload-set-stopping", workload_id)
        try:
            if info["State"]["Running"]:
                run(["docker", "stop", "--time", "20", name], timeout=40)
            run(["docker", "rm", name])
            helper_workload_command("workload-set-stopped", workload_id)
        except Exception:
            helper_workload_command("workload-set-failed", workload_id)
            raise
        return {"ok": True, "status": "stopped", "existing": True}


class AgentHandler(socketserver.StreamRequestHandler):

    def send(self, data):
        self.wfile.write(
            json.dumps(
                data,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def handle(self):
        try:
            raw = self.rfile.readline(
                MAX_REQUEST + 1
            )

            if len(raw) > MAX_REQUEST:
                self.send({
                    "ok": False,
                    "error": "request too large",
                })
                return

            request = json.loads(
                raw.decode("utf-8")
            )

            operation = request.get("op")

            if operation == "workload_start":
                self.send(workload_start(request))
                return

            if operation == "workload_status":
                self.send(workload_status(request))
                return

            if operation == "workload_stop":
                self.send(workload_stop(request))
                return

            if operation == "linux_identity_available":
                self.send(linux_identity_available(request))
                return

            if operation == "ping":
                self.send({
                    "ok": True,
                    "service": "ai-platform-agent",
                    "version": 14,
                })
                return

            if operation == "user_mount_remove":
                result = user_mount_remove(
                    request
                )

                self.send(result)
                return

            if operation == "user_mount_apply":
                result = user_mount_apply(
                    request
                )

                self.send(result)
                return

            if operation == "user_mount_status":
                result = user_mount_status(
                    request
                )

                self.send(result)
                return

            if operation == "user_encryption_status":
                result = user_encryption_status(
                    request
                )

                self.send(result)
                return

            if operation == "user_encryption_enable":
                result = user_encryption_enable(
                    request
                )

                self.send(result)
                return

            if operation == "user_encryption_change_passphrase":
                result = user_encryption_change_passphrase(
                    request
                )

                self.send(result)
                return

            if operation == "user_encryption_lock":
                result = user_encryption_lock(
                    request
                )

                self.send(result)
                return

            if operation == "user_encryption_unlock":
                result = user_encryption_unlock(
                    request
                )

                self.send(result)
                return

            if operation == "user_storage_provision":
                user_id = request.get("user_id")

                if type(user_id) is not int or user_id <= 0:
                    raise ValueError("invalid user_id")

                helper = Path(__file__).with_name(
                    "ai-platform-helper.py"
                )

                if not helper.is_file():
                    raise RuntimeError(
                        "AI Platform helper is not installed"
                    )

                result = run(
                    [
                        "python3",
                        str(helper),
                        "provision-user-full",
                        str(user_id),
                    ],
                    timeout=180,
                )

                self.send({
                    "ok": True,
                    "storage_ready": True,
                    "output": result.stdout.strip(),
                })
                return

            if operation == "tailscale_connector_provision":
                result = provision_tailscale_connector(
                    request
                )

                # Ensure the user's lightweight launcher.
                manager = Path(__file__).with_name(
                    "ai-platform-launcher-manager.py"
                )

                try:
                    launcher = run(
                        [
                            "python3",
                            str(manager),
                            str(request["connector_id"]),
                        ],
                        timeout=15,
                    )

                    result["launcher"] = (
                        launcher.stdout.strip()
                    )

                except Exception as exc:
                    # Connector provisioning succeeded.
                    # Do not destroy it if launcher setup fails.
                    result["launcher_warning"] = str(exc)

                    print(
                        "Launcher warning for connector "
                        f'{request["connector_id"]}: {exc}',
                        flush=True,
                    )

                self.send(result)
                return

            self.send({
                "ok": False,
                "error": "unsupported operation",
            })

        except Exception as exc:
            self.send({
                "ok": False,
                "error": str(exc),
            })


class AgentServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,
):
    daemon_threads = True


def main():
    if os.geteuid() != 0:
        raise SystemExit(
            "ai-platform-agent must run as root"
        )

    path = Path(SOCKET_PATH)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if path.exists():
        path.unlink()

    try:
        with AgentServer(
            SOCKET_PATH,
            AgentHandler,
        ) as server:
            os.chmod(
                SOCKET_PATH,
                0o660,
            )

            server.serve_forever()

    finally:
        if path.exists():
            path.unlink()


if __name__ == "__main__":
    main()
