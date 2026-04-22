#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env.production}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: environment file '$ENV_FILE' not found."
  echo "Copy .env.production.example to .env.production and fill in the real values first."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

echo "==> Building Plane-owned production images..."
docker compose "${COMPOSE_ARGS[@]}" build web admin space live api worker beat-worker migrator proxy

echo "==> Starting infrastructure services..."
docker compose "${COMPOSE_ARGS[@]}" up -d plane-db plane-redis plane-mq plane-minio

echo "==> Running database migrations..."
docker compose "${COMPOSE_ARGS[@]}" --profile migrate run --rm migrator

echo "==> Starting application services..."
docker compose "${COMPOSE_ARGS[@]}" up -d api worker beat-worker live web admin space proxy

echo "==> Current service status:"
docker compose "${COMPOSE_ARGS[@]}" ps

echo ""
echo "Local entrypoint health checks:"
echo "  curl -fsS http://${PLANE_BIND_HOST:-127.0.0.1}:${PLANE_PORT:-18130}/"
echo "  curl -fsS http://${PLANE_BIND_HOST:-127.0.0.1}:${PLANE_PORT:-18130}/api/instances/"
