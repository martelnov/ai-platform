#!/usr/bin/env python3

import json
import subprocess
import sys


IMAGE = "ai-platform-launcher:latest"
GATEWAY_NETWORK = "ai-gateway"
GATEWAY_API = "http://gateway-api:8080"


def run(*args, check=True):
    result = subprocess.run(
        args,
        text=True,
        capture_output=True,
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"Command failed: {args[0]}"
        )

    return result


def inspect_container(name):
    result = run(
        "docker", "inspect", name,
        check=False,
    )

    if result.returncode != 0:
        return None

    return json.loads(result.stdout)[0]



def ensure_tailscale_serve(ts_name):
    """Ensure HTTPS proxy to the user's launcher."""

    def read_config():
        result = run(
            "docker", "exec", ts_name,
            "tailscale", "serve", "status", "--json",
        )

        raw = result.stdout.strip()
        config = json.loads(raw) if raw else {}

        if config is None:
            return {}

        if not isinstance(config, dict):
            raise RuntimeError(
                "Unexpected Tailscale Serve configuration"
            )

        return config

    def is_correct(config):
        tcp = config.get("TCP") or {}
        web = config.get("Web") or {}

        if not (tcp.get("443") or {}).get("HTTPS"):
            return False

        for site in web.values():
            if not isinstance(site, dict):
                continue

            handlers = site.get("Handlers") or {}
            root = handlers.get("/") or {}

            if root.get("Proxy") == "http://127.0.0.1:8080":
                return True

        return False

    config = read_config()

    if is_correct(config):
        return

    if config:
        raise RuntimeError(
            f"{ts_name}: unexpected Serve configuration; "
            "left unchanged"
        )

    run(
        "docker", "exec", ts_name,
        "tailscale", "serve", "--bg", "8080",
    )

    if not is_correct(read_config()):
        raise RuntimeError(
            f"{ts_name}: Serve configuration failed"
        )

    print(f"{ts_name}: Serve configured")

def ensure_launcher(connector_id):
    if connector_id <= 0:
        raise ValueError("Invalid connector ID")

    ts_name = f"ai-ts-{connector_id}"
    launcher_name = f"ai-launcher-{connector_id}"

    ts = inspect_container(ts_name)

    if not ts or not ts["State"]["Running"]:
        raise RuntimeError(
            f"Tailscale connector {ts_name} is not running"
        )

    status = json.loads(
        run(
            "docker", "exec", ts_name,
            "tailscale", "status", "--json",
        ).stdout
    )

    if status.get("BackendState") != "Running":
        raise RuntimeError(
            f"Tailscale connector {ts_name} is not ready"
        )

    provider_id = status.get("Self", {}).get("ID")

    if not provider_id:
        raise RuntimeError(
            "Tailscale provider ID is missing"
        )

    # Ensure communication with central Gateway API.
    networks = ts["NetworkSettings"]["Networks"]

    if GATEWAY_NETWORK not in networks:
        run(
            "docker", "network", "connect",
            GATEWAY_NETWORK, ts_name,
        )

    # Docker stores the network container ID.
    expected_network = f"container:{ts['Id']}"

    current = inspect_container(launcher_name)

    if current:
        environment = set(
            current["Config"].get("Env") or []
        )

        correct_network = (
            current["HostConfig"]["NetworkMode"]
            == expected_network
        )

        correct_identity = (
            f"AI_CONNECTOR_PROVIDER_ID={provider_id}"
            in environment
        )

        correct_api = (
            f"AI_GATEWAY_API_URL={GATEWAY_API}"
            in environment
        )

        if (
            current["State"]["Running"]
            and correct_network
            and correct_identity
            and correct_api
        ):
            ensure_tailscale_serve(ts_name)

            print(
                f"{launcher_name}: already running"
            )
            return

    # Check image before removing an existing container.
    run(
        "docker", "image", "inspect", IMAGE,
    )

    if current:
        run(
            "docker", "rm", "-f", launcher_name,
        )

    run(
        "docker", "run", "-d",
        "--name", launcher_name,
        "--network", f"container:{ts_name}",
        "--restart", "no",
        "--user", "65532:65532",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--label", "ai.platform.component=launcher",
        "--label",
        f"ai.platform.connector_id={connector_id}",
        "-e", f"AI_CONNECTOR_PROVIDER_ID={provider_id}",
        "-e", f"AI_GATEWAY_API_URL={GATEWAY_API}",
        IMAGE,
    )

    ensure_tailscale_serve(ts_name)
    print(f"{launcher_name}: created")


def ensure_all_launchers():
    result = run(
        "docker",
        "ps",
        "--format",
        "{{.Names}}",
    )

    connector_ids = []

    for name in result.stdout.splitlines():
        if not name.startswith("ai-ts-"):
            continue

        identifier = name[6:]

        if identifier.isdigit() and int(identifier) > 0:
            connector_ids.append(int(identifier))

    failures = []

    for connector_id in sorted(set(connector_ids)):
        try:
            ensure_launcher(connector_id)
        except Exception as exc:
            failures.append(
                f"ai-ts-{connector_id}: {exc}"
            )
            print(
                f"ERROR: ai-ts-{connector_id}: {exc}",
                file=sys.stderr,
            )

    if failures:
        raise RuntimeError(
            f"{len(failures)} launcher(s) failed"
        )


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: ai-platform-launcher-manager.py "
            "CONNECTOR_ID|--all"
        )

    try:
        if sys.argv[1] == "--all":
            ensure_all_launchers()
        else:
            ensure_launcher(int(sys.argv[1]))

    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
