#!/usr/bin/env sh
# api container entrypoint. Runs once per boot, then hands off to uvicorn:
#   1. wait for Neo4j bolt (compose gates on health, but stay defensive on cold starts),
#   2. initialise the SQLite app-state tables,
#   3. restore the committed graph snapshot IF the graph is empty (stub: frozen hash vectors;
#      full: re-embed the same chunks with the baked fastembed model) — idempotent, so a populated
#      graph (a real full-mode extraction, or a re-run) is never clobbered,
#   4. exec uvicorn as PID 1's child so signals/shutdown propagate.
set -eu

echo "[api-entrypoint] mode=${PROVIDER_MODE:-stub} embed=${EMBED_BACKEND:-hash} neo4j=${NEO4J_URI:-bolt://neo4j:7687}"

python - <<'PY'
import os, time
from neo4j import GraphDatabase
uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "firmontology"))
for attempt in range(60):
    try:
        drv = GraphDatabase.driver(uri, auth=auth)
        drv.verify_connectivity()
        drv.close()
        print(f"[api-entrypoint] neo4j reachable at {uri}")
        break
    except Exception as exc:  # noqa: BLE001
        if attempt == 59:
            raise SystemExit(f"[api-entrypoint] neo4j never became reachable at {uri}: {exc}")
        time.sleep(2)
PY

echo "[api-entrypoint] initialising SQLite app state ..."
python -m api.stores.sqlite --init

echo "[api-entrypoint] restoring graph snapshot if empty ..."
python -m api.snapshot --restore

echo "[api-entrypoint] starting uvicorn on :8000 ..."
exec uvicorn api.main:app --host 0.0.0.0 --port 8000
