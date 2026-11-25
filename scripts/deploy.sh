#!/usr/bin/env bash
# Pull tagged app image from GHCR and roll out the application stack against
# externally provisioned storage (PostgreSQL / Elasticsearch / optional Redis).
#
# Usage:
#   ./scripts/deploy.sh              # latest GHCR tag
#   ./scripts/deploy.sh v0.1.0       # explicit release tag
#
# Prerequisites on the server:
#   - docker + docker compose plugin
#   - .env in the repo root (copy from .env.example; chmod 600) with
#     KB_DATABASE_URL, KB_ELASTICSEARCH_URL, optional KB_REDIS_URL, and secrets
#   - External PostgreSQL (with pgvector) and Elasticsearch (with analysis-ik)
#     already reachable from the app container (e.g. via knowledge-base-database)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_IMAGE="ghcr.io/lennylin-lab/knowledge-base-server"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
VERSION="${1:-latest}"

if [[ ! -f .env ]]; then
  echo "error: missing .env — copy .env.example and configure KB_* settings" >&2
  exit 1
fi

export KB_APP_VERSION="${VERSION}"

echo "==> Pulling ${APP_IMAGE}:${VERSION}"
docker pull "${APP_IMAGE}:${VERSION}"

echo "==> Running database migrations against external PostgreSQL"
docker compose -f "${COMPOSE_FILE}" run --rm --no-deps app alembic upgrade head

echo "==> Starting API"
docker compose -f "${COMPOSE_FILE}" up -d app

echo "==> Done. Health check:"
curl -fsS "http://localhost:${KB_APP_PORT:-8000}/healthz" || true
echo
