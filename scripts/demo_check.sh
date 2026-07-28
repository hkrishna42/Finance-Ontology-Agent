#!/usr/bin/env bash
# Smoke the running containerized stack THROUGH the web reverse proxy — proving the api is reachable
# and the Graph/Risk panels are serving live data restored from the committed snapshot (not the
# frontend's fixture fallback). Exits non-zero on any failure.
#
# Defaults to the web proxy (http://localhost:5173); override API_URL to hit the api directly.
set -euo pipefail

API="${API_URL:-http://localhost:${WEB_PORT:-5173}}"
pass() { printf '    \033[32mok\033[0m %s\n' "$1"; }
fail() { printf '\033[31mFAIL:\033[0m %s\n' "$1" >&2; exit 1; }

echo "==> stack smoke via ${API}"

# 1. health --------------------------------------------------------------------------------------
health="$(curl -fsS "${API}/health")" || fail "GET /health did not respond (is the stack up? 'make ps')"
echo "${health}" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' || fail "/health not ok: ${health}"
pass "/health = ${health}"

# 2. ontology (vector dim contract) --------------------------------------------------------------
info="$(curl -fsS "${API}/ontology/info")" || fail "GET /ontology/info failed"
echo "${info}" | grep -q '"vector_dim"[[:space:]]*:[[:space:]]*384' || fail "/ontology/info vector_dim != 384: ${info}"
pass "/ontology/info vector_dim=384"

# 3. graph — live nodes from the restored snapshot (not the UI fixture) --------------------------
graph="$(curl -fsS "${API}/graph")" || fail "GET /graph failed"
echo "${graph}" | grep -q '"nodes"' || fail "/graph missing 'nodes'"
echo "${graph}" | grep -q 'NVIDIA' || fail "/graph has no NVIDIA node — snapshot not loaded?"
echo "${graph}" | grep -q 'Demo Investment Management' || fail "/graph has no Demo Investment Management fund/firm — snapshot not loaded?"
pass "/graph returns live snapshot nodes (NVIDIA, Demo Investment Management present)"

# 4. risk — Risk Lens metrics computed over the graph --------------------------------------------
risk="$(curl -fsS "${API}/risk")" || fail "GET /risk failed"
echo "${risk}" | grep -q '"hhi"' || fail "/risk missing 'hhi'"
echo "${risk}" | grep -q '"concentration"' || fail "/risk missing 'concentration'"
pass "/risk returns concentration + HHI metrics"

echo "PASS: stack healthy; Graph + Risk panels serving live snapshot data through the proxy."
