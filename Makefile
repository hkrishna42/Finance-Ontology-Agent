# Firm Ontology Platform — developer tasks.
#
# Everything runs in containers. `make bootstrap` (stub, default) is fully offline; `make bootstrap
# MODE=full` uses real Anthropic (needs .env with ANTHROPIC_API_KEY). The host-run targets under
# "dev (host)" are an optional path for iterating without rebuilding an image.
.DEFAULT_GOAL := help
MODE ?= stub
WEB_PORT ?= 5173

COMPOSE := docker compose -f docker-compose.yml
ifeq ($(MODE),full)
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.full.yml
endif

.PHONY: help bootstrap build up down reset logs ps snapshot demo-check ci lint fmt test \
        api web seed apply-ddl models-lock

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---- containerized stack (the primary path) -------------------------------------------------
bootstrap: ## One-command bring-up: build + start neo4j+api+web, wait for health (MODE=stub|full)
	MODE=$(MODE) WEB_PORT=$(WEB_PORT) ./bootstrap.sh

build: ## Build the api + web images without starting anything
	$(COMPOSE) build

up: ## Start the (already-built) stack detached, waiting for health
	$(COMPOSE) up -d --wait

down: ## Stop and remove the containers (keeps the Neo4j + app volumes)
	docker compose -f docker-compose.yml -f docker-compose.full.yml down

reset: ## Tear everything down and delete volumes (Neo4j graph + SQLite state)
	docker compose -f docker-compose.yml -f docker-compose.full.yml down -v
	rm -f data/app.db

logs: ## Tail logs from all services
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

demo-check: ## Smoke the running stack through the web proxy (health + live snapshot panels)
	bash scripts/demo_check.sh

snapshot: ## Freeze the seed graph + hash embeddings into corpus/snapshot/ (offline; commit the diff)
	bash scripts/make_snapshot.sh

# ---- offline CI gate (host uv; no docker, no key, no Neo4j) ----------------------------------
ci: ## Offline gate: ruff + pytest (FakeProvider + HashEmbedder; DB-backed tests self-skip)
	uv run ruff check .
	PROVIDER_MODE=stub EMBED_BACKEND=hash uv run pytest

lint: ## ruff + mypy
	uv run ruff check .
	uv run mypy api

fmt: ## Auto-fix lint + format
	uv run ruff check . --fix
	uv run ruff format .

test: ## Run the offline test suite
	uv run pytest

models-lock: ## Regenerate models.lock from the routing registry
	uv run python scripts/write_models_lock.py

# ---- dev (host): optional, for iterating without rebuilding an image -------------------------
# Requires `uv sync` and a reachable Neo4j (e.g. `docker compose up -d neo4j`).
api: ## [dev] Run the API on the host (uvicorn, reload) against localhost Neo4j
	PROVIDER_MODE=$(MODE) NEO4J_URI=bolt://localhost:7687 uv run uvicorn api.main:app --reload --port 8000

web: ## [dev] Run the Vite dev server on the host
	cd web && npm run dev

apply-ddl: ## [dev] Apply Neo4j DDL + verify the vector index ONLINE (localhost Neo4j)
	NEO4J_URI=bolt://localhost:7687 uv run python scripts/apply_ddl.py

seed: ## [dev] Load the seed graph + embed chunks into localhost Neo4j
	NEO4J_URI=bolt://localhost:7687 uv run python scripts/load_seed.py --stats
