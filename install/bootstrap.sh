#!/usr/bin/env bash
set -Eeuo pipefail

DATA_ROOT="/AI/data"
MODELS_ROOT="/AI/models"
NODE_NAME="$(hostname -s)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

log() {
    echo
    echo "==> $*"
}

if [[ $EUID -ne 0 ]]; then
    die "run as root"
fi

if [[ ! -r /etc/os-release ]]; then
    die "/etc/os-release not found"
fi

. /etc/os-release

[[ "${ID:-}" == "ubuntu" ]] || die "only Ubuntu is currently supported"

case "${VERSION_CODENAME:-}" in
    noble|resolute)
        ;;
    *)
        die "supported Ubuntu releases are currently 24.04 (noble) and 26.04 (resolute)"
        ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root)
            DATA_ROOT="$2"
            shift 2
            ;;
        --models-root)
            MODELS_ROOT="$2"
            shift 2
            ;;
        --node-name)
            NODE_NAME="$2"
            shift 2
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

log "Checking storage"

for path in "$DATA_ROOT" "$MODELS_ROOT"; do
    [[ -d "$path" ]] || die "storage path does not exist: $path"
    mountpoint -q "$path" || die "storage path is not a mountpoint: $path"
done

log "Installing required host packages"

apt-get update
apt-get install -y \
    ca-certificates \
    cifs-utils \
    curl \
    nfs-common \
    openssl \
    python3-cryptography \
    python3-minimal \
    quota

if command -v docker >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 ||
        die "Docker exists but docker compose plugin is unavailable"
else
    log "Installing Docker Engine"

    apt-get remove -y \
        docker.io \
        docker-compose \
        docker-compose-v2 \
        docker-doc \
        docker-buildx \
        podman-docker \
        containerd \
        runc 2>/dev/null || true

    install -m 0755 -d /etc/apt/keyrings

    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc

    chmod a+r /etc/apt/keyrings/docker.asc

    cat >/etc/apt/sources.list.d/docker.sources <<DOCKER_REPO
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPO

    apt-get update

    apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin
fi

systemctl enable --now docker

log "Creating platform configuration"

install -d -m 0755 /etc/ai-platform
install -d -m 0700 /etc/ai-platform/secrets

if [[ ! -s /etc/ai-platform/secrets/postgres_password ]]; then
    umask 077
    openssl rand -base64 36 \
        > /etc/ai-platform/secrets/postgres_password
fi

if [[ ! -s /etc/ai-platform/secrets/master.key ]]; then
    umask 077
    openssl rand -out /etc/ai-platform/secrets/master.key 32
fi

if [[ "$(stat -c %s /etc/ai-platform/secrets/master.key)" -ne 32 ]]; then
    die "master.key must be exactly 32 bytes"
fi

cat >/etc/ai-platform/config.toml <<CONFIG
[platform]
repo_root = "$REPO_ROOT"

[node]
name = "$NODE_NAME"

[paths]
data_root = "$DATA_ROOT"
models_root = "$MODELS_ROOT"

[database]
host = "postgres"
port = 5432
name = "ai_platform"
user = "ai_platform"
password_file = "/run/secrets/postgres_password"
CONFIG

cat >/etc/ai-platform/compose.env <<CONFIG
AI_DATA_ROOT=$DATA_ROOT
AI_MODELS_ROOT=$MODELS_ROOT
CONFIG

chmod 600 \
    /etc/ai-platform/config.toml \
    /etc/ai-platform/compose.env \
    /etc/ai-platform/secrets/postgres_password \
    /etc/ai-platform/secrets/master.key

install -d "$DATA_ROOT/postgres"
install -d "$DATA_ROOT/users"
install -d "$DATA_ROOT/connectors/tailscale"
install -d "$DATA_ROOT/platform"
install -d "$DATA_ROOT/backups"

install -d "$MODELS_ROOT/common"
install -d "$MODELS_ROOT/users"

COMPOSE=(
    docker compose
    --env-file /etc/ai-platform/compose.env
    -f "$REPO_ROOT/compose.yml"
)

log "Starting PostgreSQL"

"${COMPOSE[@]}" pull postgres
"${COMPOSE[@]}" up -d postgres

for i in $(seq 1 60); do
    if "${COMPOSE[@]}" exec -T postgres \
        pg_isready -U ai_platform -d ai_platform \
        >/dev/null 2>&1; then
        break
    fi

    [[ "$i" -lt 60 ]] || die "PostgreSQL did not become ready"
    sleep 2
done

migration_applied() {
    local version="$1"
    local result

    result="$(
        "${COMPOSE[@]}" exec -T postgres \
            psql -X -A -t \
            -U ai_platform \
            -d ai_platform \
            -c "SELECT 1 FROM schema_migrations WHERE version=$version;" \
            2>/dev/null || true
    )"

    [[ "$result" == "1" ]]
}

log "Applying database migrations"

for migration in "$REPO_ROOT"/db/migrations/*.sql; do
    filename="$(basename "$migration")"
    version="${filename%%_*}"

    version=$((10#$version))

    if migration_applied "$version"; then
        echo "    $filename already applied"
        continue
    fi

    echo "    applying $filename"

    "${COMPOSE[@]}" exec -T postgres \
        psql -X -v ON_ERROR_STOP=1 \
        -U ai_platform \
        -d ai_platform \
        < "$migration"
done

log "Registering host and local storage"

HOSTNAME_SHORT="$(hostname -s)"

"${COMPOSE[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 \
    -U ai_platform \
    -d ai_platform \
    -v node_name="$NODE_NAME" \
    -v host_name="$HOSTNAME_SHORT" <<'SQL'
INSERT INTO hosts (name, hostname)
VALUES (:'node_name', :'host_name')
ON CONFLICT (name)
DO UPDATE SET
    hostname = EXCLUDED.hostname,
    updated_at = now();

INSERT INTO storage_resources
    (host_id, name, backend_type, source_ref)
SELECT
    h.id,
    'local-data',
    'directory',
    'bootstrap:data_root'
FROM hosts h
WHERE h.name = :'node_name'
  AND NOT EXISTS (
      SELECT 1
      FROM storage_resources s
      WHERE s.name = 'local-data'
  );

INSERT INTO storage_resources
    (host_id, name, backend_type, source_ref)
SELECT
    h.id,
    'local-models',
    'directory',
    'bootstrap:models_root'
FROM hosts h
WHERE h.name = :'node_name'
  AND NOT EXISTS (
      SELECT 1
      FROM storage_resources s
      WHERE s.name = 'local-models'
  );
SQL

log "Installing host helper"

install -m 0750 -o root -g root \
    "$REPO_ROOT/host/ai-platform-helper.py" \
    /usr/local/sbin/ai-platform-helper

log "Checking filesystem quotas"

quota_ok=1

for path in "$DATA_ROOT" "$MODELS_ROOT"; do
    output="$(LC_ALL=C quotaon -p -u "$path" 2>&1 || true)"

    if grep -qi ' is on' <<<"$output"; then
        echo "    $path: user quota on"
    else
        echo "    $path: USER QUOTA NOT ACTIVE"
        quota_ok=0
    fi
done

log "Installing host agent"

install -D -m 0755 \
    "$REPO_ROOT/host/ai-platform-agent.py" \
    /usr/local/lib/ai-platform/ai-platform-agent.py

install -m 0644 \
    "$REPO_ROOT/install/systemd/ai-platform-agent.service" \
    /etc/systemd/system/ai-platform-agent.service

systemctl daemon-reload
systemctl enable --now ai-platform-agent

echo
echo "AI Platform bootstrap complete"
echo "Node:        $NODE_NAME"
echo "Repository:  $REPO_ROOT"
echo "Data:        $DATA_ROOT"
echo "Models:      $MODELS_ROOT"

if [[ "$quota_ok" -eq 0 ]]; then
    echo
    echo "WARNING: filesystem quota support still needs to be enabled."
fi
