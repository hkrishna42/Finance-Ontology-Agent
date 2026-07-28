#!/usr/bin/env bash
# QA promotion gate for the Firm Ontology Platform.
#
# Runs the full QA surface and exits NON-ZERO on any failure, so it can gate every workstream's
# promotion. Stages:
#   0. lint            — ruff check .
#   1. adversarial     — uv run pytest tests/adversarial   (hero/guard/ontology/provenance)
#   2. full suite      — uv run pytest                     (adversarial + frozen offline tests)
#
# The DB-backed invariants (hero queries, provenance, live vector index) need a *seeded* Neo4j.
# By default this script brings up the worktree-isolated Neo4j (bolt 7690 / http 7477), applies
# the DDL, seeds, runs the gate, and tears the DB back down (honouring "keep Neo4j down when
# idle"). Escapes:
#   SKIP_DB=1     run only the offline stages; DB-backed tests self-skip (fast, no docker).
#   KEEP_NEO4J=1  leave Neo4j running after the gate (for debugging).
#
# Usage:  bash scripts/qa_check.sh
set -uo pipefail

# --- Locate repo root (this script lives in <root>/scripts) --------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

# --- Isolated connection env (overridable) -------------------------------------------------
export NEO4J_BOLT_PORT="${NEO4J_BOLT_PORT:-7690}"
export NEO4J_HTTP_PORT="${NEO4J_HTTP_PORT:-7477}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:${NEO4J_BOLT_PORT}}"
export NEO4J_USER="${NEO4J_USER:-neo4j}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-firmontology}"
export PROVIDER_MODE="${PROVIDER_MODE:-stub}"
export EMBED_BACKEND="${EMBED_BACKEND:-hash}"

SKIP_DB="${SKIP_DB:-0}"
KEEP_NEO4J="${KEEP_NEO4J:-0}"
CONTAINER="fop-qa-neo4j-1"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
fail=0
declare -a RESULTS

stage() { printf '\n%s== %s ==%s\n' "${BOLD}" "$1" "${RST}"; }
record() { # name, rc
  if [ "$2" -eq 0 ]; then RESULTS+=("${GRN}PASS${RST} $1"); else RESULTS+=("${RED}FAIL${RST} $1"); fail=1; fi
}

teardown() {
  if [ "${SKIP_DB}" != "1" ] && [ "${KEEP_NEO4J}" != "1" ]; then
    stage "teardown: docker compose down"
    docker compose down >/dev/null 2>&1 || true
  fi
}
trap teardown EXIT

# --- DB bring-up ---------------------------------------------------------------------------
if [ "${SKIP_DB}" != "1" ]; then
  if ! docker info >/dev/null 2>&1; then
    printf '%sdocker is not available; re-run with SKIP_DB=1 for the offline gate only.%s\n' "${YEL}" "${RST}"
    exit 2
  fi
  stage "infra: docker compose up -d neo4j (bolt ${NEO4J_BOLT_PORT})"
  docker compose up -d neo4j || { echo "${RED}failed to start Neo4j${RST}"; exit 2; }

  printf 'waiting for Neo4j to be healthy'
  ok=0
  for _ in $(seq 1 40); do
    hs="$(docker inspect --format '{{.State.Health.Status}}' "${CONTAINER}" 2>/dev/null || echo none)"
    if [ "${hs}" = "healthy" ]; then ok=1; printf ' healthy\n'; break; fi
    printf '.'; sleep 3
  done
  [ "${ok}" -eq 1 ] || { printf '\n%sNeo4j never became healthy%s\n' "${RED}" "${RST}"; exit 2; }

  stage "schema: apply DDL (+ verify vector index ONLINE)"
  uv run python scripts/apply_ddl.py; record "apply-ddl" $?

  stage "seed: load demo graph + embed chunks"
  uv run python scripts/load_seed.py --stats; record "seed" $?
else
  printf '%sSKIP_DB=1 — DB-backed invariants will self-skip.%s\n' "${YEL}" "${RST}"
fi

# --- Stage 0: lint -------------------------------------------------------------------------
stage "lint: ruff check ."
uv run ruff check .; record "ruff" $?

# --- Stage 1: adversarial suite (the QA-owned gate) ----------------------------------------
stage "adversarial: uv run pytest tests/adversarial"
uv run pytest tests/adversarial -q; record "pytest tests/adversarial" $?

# --- Stage 2: full suite (adversarial + frozen offline contracts) --------------------------
stage "full suite: uv run pytest"
uv run pytest -q; record "pytest (full)" $?

# --- Summary -------------------------------------------------------------------------------
stage "summary"
for line in "${RESULTS[@]}"; do printf '  %s\n' "${line}"; done
if [ "${fail}" -ne 0 ]; then
  printf '\n%sQA GATE: FAIL%s\n' "${RED}${BOLD}" "${RST}"
  exit 1
fi
printf '\n%sQA GATE: PASS%s\n' "${GRN}${BOLD}" "${RST}"
exit 0
