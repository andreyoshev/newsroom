#!/usr/bin/env bash
# Build the images, start the stack, and check that it came up.
# Safe to re-run: there is no state in this script, and none in the environment
# either — the service configures itself from its own web UI on first run.
#
# Usage:
#   ./deploy.sh                # build + up + healthcheck
#   ./deploy.sh --pull         # docker compose pull first (for image: services)
#   ./deploy.sh --help
#
# Where the web service listens on the host comes from NEWSROOM_BIND and
# defaults to 127.0.0.1:18080, for a tunnel or reverse proxy on this machine.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BIND="${NEWSROOM_BIND:-127.0.0.1:18080}"

# --- output helpers ---
if [ -t 1 ]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi
info()  { printf '%s==>%s %s\n' "$CYAN$BOLD" "$RESET" "$*"; }
ok()    { printf '%s ok%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%swarn%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()   { printf '%serr%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

PULL=0
for arg in "$@"; do
    case "$arg" in
        --pull)     PULL=1 ;;
        --help|-h)
            sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown flag: $arg (try --help)" ;;
    esac
done

# --- prerequisites ---
info "checking prerequisites"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose v2 is not available — install: sudo apt-get install -y docker-compose-plugin"
command -v curl >/dev/null || die "curl is required for the healthcheck"
ok "docker, compose and curl are present"

# --- build & up ---
if [ "$PULL" -eq 1 ]; then
    info "docker compose pull"
    docker compose pull
fi

info "docker compose up -d --build --remove-orphans"
NEWSROOM_BIND="$BIND" docker compose up -d --build --remove-orphans

# --- healthcheck ---
PROBE_HOST="${BIND%:*}"
PROBE_PORT="${BIND##*:}"
case "$PROBE_HOST" in
    ""|0.0.0.0|"::"|"[::]") PROBE_HOST="127.0.0.1" ;;
esac
HEALTH_URL="http://${PROBE_HOST}:${PROBE_PORT}/healthz"
SETUP_URL="http://${PROBE_HOST}:${PROBE_PORT}/setup"

info "waiting for $HEALTH_URL"
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" || true)
    if [ "$code" = "200" ]; then
        ok "healthz answered 200"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo
        docker compose logs --tail 50 web || true
        die "healthz did not come up within 30s (logs above)"
    fi
    sleep 1
done

info "checking whether setup has been completed"
setup_code=$(curl -s -o /dev/null -w "%{http_code}" "$SETUP_URL" || true)
if [ "$setup_code" = "200" ]; then
    FRESH=1
else
    FRESH=0
    mcp_code=$(curl -s -o /dev/null -w "%{http_code}" "http://${PROBE_HOST}:${PROBE_PORT}/mcp" || true)
    [ "$mcp_code" = "401" ] || warn "expected 401 from /mcp without a token, got $mcp_code"
fi

# --- summary ---
echo
if [ "$FRESH" -eq 1 ]; then
    cat <<EOF
${BOLD}Running, and waiting to be set up.${RESET}

Point your tunnel or reverse proxy at ${BIND}, then open the service in a
browser. Every path redirects to the one-time setup page, where you create the
admin account and enter the public address people will reach this at.

  locally:  ${SETUP_URL}
EOF
else
    cat <<EOF
${BOLD}Running.${RESET} It is already set up — open the dashboard to sign in.
Settings (public address, invite code, accounts) live under ${BOLD}Settings${RESET} in the UI.
EOF
fi
cat <<EOF

${BOLD}Logs:${RESET} docker compose logs -f web wacli-supervisor
EOF
