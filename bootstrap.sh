#!/usr/bin/env bash
# bootstrap.sh — one-command containerized bring-up for the Firm Ontology Platform.
#
#   MODE=stub  (default) : fully offline. Builds images, starts neo4j+api+web, and the api restores
#                          the committed graph snapshot on first boot. No API key, no network models.
#   MODE=full            : real Anthropic extraction/narration + fastembed. Needs .env with a funded
#                          ANTHROPIC_API_KEY (see .env.example).
#
# Everything runs in containers — no host uvicorn/vite. Re-runnable and idempotent; `up --wait`
# gates on health, so this returns only once the whole stack is actually serving.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODE="${MODE:-stub}"
WEB_PORT="${WEB_PORT:-5173}"
NEO4J_HTTP_PORT="${NEO4J_HTTP_PORT:-7474}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-600}"

log()  { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[bootstrap] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- docker present + daemon up -------------------------------------------------------------
command -v docker >/dev/null 2>&1 \
  || die "docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
docker info >/dev/null 2>&1 \
  || die "the Docker daemon isn't running. Start Docker Desktop, wait for it to settle, then retry."

# ---- compose file selection by mode ---------------------------------------------------------
COMPOSE=(docker compose -f docker-compose.yml)
if [[ "$MODE" == "full" ]]; then
  COMPOSE+=(-f docker-compose.full.yml)
  if [[ ! -f .env ]] || ! grep -qE '^ANTHROPIC_API_KEY=.+' .env; then
    die "MODE=full needs a funded key. Run: cp .env.example .env  then set ANTHROPIC_API_KEY=sk-ant-..."
  fi
  log "mode=full — real Claude extraction/narration, fastembed embeddings (key from .env)."
elif [[ "$MODE" == "stub" ]]; then
  log "mode=stub — fully offline; the api restores the committed snapshot. No API key needed."
else
  die "unknown MODE='$MODE' (expected 'stub' or 'full')."
fi

# ---- free ports left by any prior HOST-mode run (best effort) -------------------------------
pkill -f "uvicorn api.main" >/dev/null 2>&1 || true
pkill -f "node.*vite" >/dev/null 2>&1 || true

# ---- build + up (gates on health) -----------------------------------------------------------
log "building images + starting neo4j → api → web (first build can take a few minutes) ..."
"${COMPOSE[@]}" up --build --wait --wait-timeout "$WAIT_TIMEOUT" || {
  warn "stack did not become healthy within ${WAIT_TIMEOUT}s. Recent logs:"
  "${COMPOSE[@]}" ps || true
  "${COMPOSE[@]}" logs --tail=40 api || true
  die "bring-up failed. See 'make logs' for the full output."
}

echo
log "stack healthy."
log "  Web UI : http://localhost:${WEB_PORT}"
log "  Neo4j  : http://localhost:${NEO4J_HTTP_PORT}   (neo4j / firmontology)"
log "  Verify : bash scripts/demo_check.sh"
log "Stop: make down   |   Reset volumes: make reset   |   Logs: make logs"
