#!/usr/bin/env bash
set -Eeuo pipefail

DATA_ROOT="/AI/data"
MODELS_ROOT="/AI/models"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

failures=0
warnings=0

ok() {
    printf '  [OK]   %s\n' "$*"
}

warn() {
    printf '  [WARN] %s\n' "$*"
    warnings=$((warnings + 1))
}

fail() {
    printf '  [FAIL] %s\n' "$*"
    failures=$((failures + 1))
}

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
        *)
            echo "ERROR: unknown option: $1" >&2
            exit 2
            ;;
    esac
done

echo "AI Platform preflight"
echo "Repository: $REPO_ROOT"
echo

echo "Operating system"

if [[ -r /etc/os-release ]]; then
    . /etc/os-release

    if [[ "${ID:-}" == "ubuntu" ]]; then
        ok "${PRETTY_NAME:-Ubuntu}"
    else
        fail "unsupported OS: ${PRETTY_NAME:-unknown}"
    fi
else
    fail "/etc/os-release missing"
fi

echo
echo "Storage"

for path in "$DATA_ROOT" "$MODELS_ROOT"; do
    if [[ ! -d "$path" ]]; then
        fail "$path does not exist"
        continue
    fi

    if ! mountpoint -q "$path"; then
        fail "$path is not a mountpoint"
        continue
    fi

    source="$(findmnt -n -o SOURCE "$path")"
    fstype="$(findmnt -n -o FSTYPE "$path")"

    ok "$path -> $source ($fstype)"

    if [[ "$fstype" != "ext4" ]]; then
        warn "$path is not ext4; quota backend will need review"
    fi

    quota_state="$(LC_ALL=C quotaon -p -u "$path" 2>&1 || true)"

    if grep -qi ' is on' <<<"$quota_state"; then
        ok "$path user quota is active"
    else
        warn "$path user quota is not active"
    fi
done

echo
echo "Docker"

if command -v docker >/dev/null 2>&1; then
    ok "$(docker --version)"
else
    fail "Docker is not installed"
fi

if docker compose version >/dev/null 2>&1; then
    ok "$(docker compose version)"
else
    fail "Docker Compose plugin is unavailable"
fi

echo
echo "Platform configuration"

for file in \
    /etc/ai-platform/config.toml \
    /etc/ai-platform/compose.env \
    /etc/ai-platform/secrets/postgres_password \
    /etc/ai-platform/secrets/master.key
do
    if [[ -f "$file" ]]; then
        ok "$file"
    else
        warn "$file missing"
    fi
done

COMPOSE_ENV="/etc/ai-platform/compose.env"

if [[ -f "$COMPOSE_ENV" ]]; then
    if docker compose \
        --env-file "$COMPOSE_ENV" \
        -f "$REPO_ROOT/compose.yml" \
        config --quiet
    then
        ok "compose configuration is valid"
    else
        fail "compose configuration is invalid"
    fi
else
    warn "cannot validate compose with host environment"
fi

echo
echo "Cryptography"

if python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' 2>/dev/null; then
    ok "Python AES-GCM support available"
else
    fail "python3-cryptography / AESGCM unavailable"
fi

echo
echo "PostgreSQL"

if [[ -f "$COMPOSE_ENV" ]] &&
   docker compose \
       --env-file "$COMPOSE_ENV" \
       -f "$REPO_ROOT/compose.yml" \
       exec -T postgres \
       pg_isready -U ai_platform -d ai_platform \
       >/dev/null 2>&1
then
    ok "PostgreSQL is ready"

    migrations="$(
        docker compose \
            --env-file "$COMPOSE_ENV" \
            -f "$REPO_ROOT/compose.yml" \
            exec -T postgres \
            psql -X -A -t \
            -U ai_platform \
            -d ai_platform \
            -c \
            "SELECT string_agg(version::text, ',' ORDER BY version)
             FROM schema_migrations;" \
            2>/dev/null || true
    )"

    if [[ -n "$migrations" ]]; then
        ok "database migrations: $migrations"
    else
        warn "cannot determine database migration state"
    fi
else
    warn "PostgreSQL is not currently reachable"
fi

echo
echo "Host helper"

if [[ -x /usr/local/sbin/ai-platform-helper ]]; then
    ok "/usr/local/sbin/ai-platform-helper"
else
    warn "ai-platform-helper is not installed"
fi


echo
echo "Host agent"

if systemctl is-enabled --quiet ai-platform-agent 2>/dev/null; then
    ok "ai-platform-agent enabled"
else
    warn "ai-platform-agent not enabled"
fi

if systemctl is-active --quiet ai-platform-agent; then
    ok "ai-platform-agent active"
else
    fail "ai-platform-agent not active"
fi

if [[ -S /run/ai-platform/agent.sock ]]; then
    ok "/run/ai-platform/agent.sock"
else
    fail "host agent socket missing"
fi


echo
echo "Summary"
echo "  failures: $failures"
echo "  warnings: $warnings"

if [[ "$failures" -gt 0 ]]; then
    exit 1
fi

exit 0
