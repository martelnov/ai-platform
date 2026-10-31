#!/usr/bin/env bash
set -Eeuo pipefail

CONTROL_PLANE_URL="http://127.0.0.1:8080"
CONTROL_PLANE_PORT="8080"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

log() {
    echo "==> $*"
}

[[ $EUID -eq 0 ]] || die "run as root"

command -v tailscale >/dev/null 2>&1 ||
    die "Tailscale is not installed"

systemctl is-active --quiet tailscaled ||
    die "tailscaled is not running"

tailscale status >/dev/null 2>&1 ||
    die "Tailscale node is not connected"

log "Checking control plane"

curl -fsS "${CONTROL_PLANE_URL}/health" >/dev/null ||
    die "control plane is not healthy on ${CONTROL_PLANE_URL}"

log "Configuring Tailscale Serve"

tailscale serve --bg "${CONTROL_PLANE_PORT}"

echo
tailscale serve status
