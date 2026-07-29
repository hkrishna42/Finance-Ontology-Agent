"""FastAPI app — health, ontology introspection, and a canned ingest SSE demo stream.

M0 surface only (enough for the web skeleton to render a real API + a real event stream):
  GET /health              -> {status, mode}
  GET /ontology/info       -> OntologyInfo counts
  GET /ontology/schema     -> extraction_json_schema()
  GET /ontology/ddl        -> {statements: neo4j_ddl()}
  GET /ontology/card       -> {card: schema_card()}
  GET /ingest/demo/stream  -> SSE: job.started → classified → parsed → chunked → extracted →
                              written → job.completed  (canned SSEEvents; real stream shape)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from .config import get_settings
from .contracts.events import EventType, make_event
from .fibo.routes import router as fibo_router
from .firms import graph_ops as firms_graph_ops
from .firms import store as firms_store
from .firms.routes import router as firms_router
from .graph_view import router as graph_router
from .ingest.routes import router as ingest_router
from .lakehouse.routes import router as lakehouse_router
from .mdm.routes import router as mdm_router

# Application module routers (M5–M7). The graph/query routers (M2–M3) are wired the same way
# when that workstream merges.
from .modules.impact_routes import router as impact_router
from .modules.reg_routes import router as reg_router
from .modules.risk_routes import router as risk_router
from .onboarding.routes import router as onboarding_router
from .ontology.schema import (
    OntologyInfo,
    extraction_json_schema,
    neo4j_ddl,
    schema_card,
)
from .query.routes import router as query_router
from .resolution.routes import router as resolve_router
from .stores.neo4j import Neo4jStore
from .stores.sqlite import connect, init_db


def _sync_firm_registry() -> None:
    """Best-effort startup sync of the firm registry (contract §E).

    Mirrors the graph's firms into the SQLite registry, then guarantees the demo firm is registered
    with exactly one firm active. A missing SQLite file or an unreachable Neo4j only logs a warning:
    the API (health, ontology, the demo stream) must boot even with no databases attached, so this
    never raises. `ensure_demo_firm` is pure SQLite and still runs when the graph sync is skipped.
    """
    log = logging.getLogger("api.firms")
    settings = get_settings()
    try:
        conn = connect(settings.sqlite_path)
        init_db(conn)
    except Exception as exc:  # SQLite unavailable → nothing to register
        log.warning("firm registry startup: SQLite unavailable, skipping (%s)", exc)
        return
    try:
        store = Neo4jStore(settings)
        try:
            firms_store.sync_from_graph(conn, firms_graph_ops.list_graph_firms(store))
        finally:
            store.close()
    except Exception as exc:  # graph unreachable → still ensure the demo firm below
        log.warning("firm registry startup: graph sync skipped (%s)", exc)
    try:
        firms_store.ensure_demo_firm(conn)
    except Exception as exc:
        log.warning("firm registry startup: ensure_demo_firm skipped (%s)", exc)
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the firm-registry sync once on startup, then hand control to the app."""
    _sync_firm_registry()
    yield


app = FastAPI(title="Firm Ontology Platform API", version="0.1.0", lifespan=lifespan)

# Open CORS for any local Vite dev origin (any localhost port).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(risk_router)
app.include_router(impact_router)
app.include_router(reg_router)
app.include_router(graph_router)
app.include_router(query_router)
app.include_router(resolve_router)
app.include_router(ingest_router)
app.include_router(firms_router)
app.include_router(onboarding_router)
app.include_router(fibo_router)
app.include_router(lakehouse_router)
app.include_router(mdm_router)


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "mode": settings.provider_mode}


@app.get("/ontology/info")
def ontology_info() -> dict:
    return asdict(OntologyInfo())


@app.get("/ontology/schema")
def ontology_schema() -> dict:
    return extraction_json_schema()


@app.get("/ontology/ddl")
def ontology_ddl() -> dict:
    return {"statements": neo4j_ddl()}


@app.get("/ontology/card")
def ontology_card() -> dict:
    return {"card": schema_card()}


# -- demo SSE stream ----------------------------------------------------------------------

# Canned pipeline sequence: (event type, data payload).
_DEMO_SEQUENCE: list[tuple[EventType, dict]] = [
    (EventType.JOB_STARTED, {"doc_id": "nvda_10k", "source": "demo"}),
    (EventType.CLASSIFIED, {"doc_type": "10-K", "sensitivity": "public"}),
    (EventType.PARSED, {"pages": 42}),
    (EventType.CHUNKED, {"chunks": 128}),
    (EventType.EXTRACTED, {"entities": 37, "relations": 21, "dropped": 4}),
    (EventType.WRITTEN, {"nodes": 33, "edges": 19, "rejected": 2}),
    (EventType.JOB_COMPLETED, {"ok": True}),
]


async def _demo_events(job_id: str) -> AsyncIterator[dict]:
    for seq, (etype, data) in enumerate(_DEMO_SEQUENCE):
        yield make_event(etype, job_id, seq=seq, **data).to_sse()
        await asyncio.sleep(0.4)


@app.get("/ingest/demo/stream")
async def ingest_demo_stream() -> EventSourceResponse:
    job_id = f"demo-{uuid.uuid4().hex[:8]}"
    return EventSourceResponse(_demo_events(job_id))
