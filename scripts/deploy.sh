#!/usr/bin/env bash
# Pull tagged images from GHCR and roll out the production stack.
#
# Usage:
#   ./scripts/deploy.sh              # latest GHCR tag
#   ./scripts/deploy.sh v0.1.0       # explicit release tag
#
# Prerequisites on the server:
#   - docker + docker compose plugin
#   - .env in the repo root (copy from .env.example; chmod 600)
#   - vm.max_map_count >= 262144 (Elasticsearch on Linux)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_IMAGE="ghcr.io/lennylin-lab/knowledge-base-server"
ES_IMAGE="ghcr.io/lennylin-lab/knowledge-base-server/elasticsearch:8.17.3-ik"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
VERSION="${1:-latest}"

if [[ ! -f .env ]]; then
  echo "error: missing .env — copy .env.example and configure KB_* secrets" >&2
  exit 1
fi

export KB_APP_VERSION="${VERSION}"

echo "==> Pulling ${APP_IMAGE}:${VERSION}"
docker pull "${APP_IMAGE}:${VERSION}"
echo "==> Pulling ${ES_IMAGE}"
docker pull "${ES_IMAGE}"

echo "==> Starting datastore services"
docker compose -f "${COMPOSE_FILE}" up -d postgres elasticsearch redis

echo "==> Running database migrations"
docker compose -f "${COMPOSE_FILE}" run --rm --no-deps app alembic upgrade head

echo "==> Starting API"
docker compose -f "${COMPOSE_FILE}" up -d app

echo "==> Done. Health check:"
curl -fsS "http://localhost:${KB_APP_PORT:-8000}/healthz" || true
echo
