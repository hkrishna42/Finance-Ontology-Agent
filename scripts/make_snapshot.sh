#!/usr/bin/env bash
# Freeze the seed graph + deterministic HashEmbedder :Chunk vectors into corpus/snapshot/
# (committed to git). Fully offline — no Neo4j, no network, no model download. The api container
# restores this on first boot in stub mode (see api/snapshot.py :: restore_if_empty).
#
# Re-run after editing fixtures/seed_graph.cypher; commit the resulting corpus/snapshot/ diff.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
uv run python -m api.snapshot --write
