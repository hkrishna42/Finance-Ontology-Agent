"""The onboarding orchestrator — `onboard_firm(...)` turns a picked firm into a live graph + registry.

Given a candidate the user picked (`{name, cik?, lei?, series:[{series_id, name}]}`), this runs the
DETERMINISTIC, network-only onboarding flow and YIELDS the `SSEEvent` onboarding sequence as it goes:

    job.started → per series (parsed → written) → resolved → job.completed

and, as side effects:

  * MERGEs the firm `Company` node (cik/lei) and registers it as `status="onboarding"`;
  * for each series: fetches the latest N-PORT-P XML (`discovery.fetch_series_nport_xml`), parses it
    (`api.l2.nport.parse_nport`), writes the `Fund` + weighted `HOLDS` edges (`api.l2.nport.write`),
    then links `(Fund)-[:MANAGED_BY]->(Company)`;
  * resolves each distinct holding issuer onto the SEC/GLEIF spine (the reused offline `Resolver`
    with a `FakeProvider` adjudicator — no LLM/key) and enriches the issuer `Company` nodes;
  * flips the registry row to `status="ready"` with its funds/identifiers and ACTIVATES it.

Every seam (`store`, registry `conn`, `resolver`) is injectable so the whole thing runs offline in
tests with a fake store; `discovery` is imported lazily and called by attribute so tests can stub it.
Only extraction-free structural work happens here — onboarding spends zero LLM tokens.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from ..contracts.events import EventType, SSEEvent, make_event
from ..firms import store as firms_store
from ..l2 import nport
from .enrich import enrich_firm


def onboard_firm(
    picked: dict[str, Any],
    *,
    settings: Any = None,
    store: Any = None,
    conn: Any = None,
    resolver: Any = None,
    enrich: bool = True,
    provider: Any = None,
) -> Iterator[SSEEvent]:
    """Onboard the firm described by `picked`, yielding SSE events in contract order.

    `picked = {name, cik?, lei?, series: [{series_id, name}]}`. `store` defaults to a real
    `Neo4jStore` (owned + closed here) and `conn` to the SQLite firm registry at
    `settings.sqlite_path`; inject a fake store / temp connection for offline tests. `resolver`
    defaults to the deterministic `$0` resolver (real spine, `FakeProvider` adjudicator).

    When `enrich` (default True), after the firm reaches `status="ready"` the deterministic
    structural graph is layered with semantic filing data: `enrich_firm(name, ...)` runs and its SSE
    events are FORWARDED as a final "enriching" stage (stamped `stage="enriching"`), so the firm's
    Analyst-Chat / Documents / Risk-factor features get firm-specific `Chunk`/`RiskFactor` data
    instead of being empty. It is best-effort — the firm is already structurally onboarded, so an
    enrichment failure emits a single non-fatal `error` event and the run still completes. The stream
    keeps exactly one leading `job.started` and one terminal `job.completed`, whose payload carries
    the enrichment counts under `enrichment`. `provider` is the LLM seam forwarded to enrichment
    (default `stub`/`FakeProvider`); the structural onboarding itself spends zero LLM tokens.

    On any hard (structural) failure the registry row is flipped to `status="failed"`, an `ERROR`
    event is emitted, and the exception re-raised (the route's worker surfaces it on the stream).
    Idempotent: every graph and registry write is a MERGE/upsert, so re-onboarding never duplicates.
    """
    from ..config import get_settings

    settings = settings or get_settings()

    name = str(picked.get("name") or "").strip()
    if not name:
        raise ValueError("onboard_firm: picked.name is required")
    cik = picked.get("cik")
    lei = picked.get("lei")
    series: list[dict[str, Any]] = list(picked.get("series") or [])
    job_id = f"onboard-{firms_store.slugify(name)}-{uuid4().hex[:6]}"

    own_store = store is None
    if own_store:
        from ..stores.neo4j import Neo4jStore

        store = Neo4jStore(settings)

    own_conn = conn is None
    if own_conn:
        from ..stores.sqlite import connect, init_db

        conn = init_db(connect(settings.sqlite_path))

    seq = 0

    def emit(event_type: EventType, **data: Any) -> SSEEvent:
        nonlocal seq
        ev = make_event(event_type, job_id, seq=seq, **data)
        seq += 1
        return ev

    funds_written: list[dict[str, Any]] = []
    issuers: dict[str, str | None] = {}  # issuer name -> first-seen ticker (order-preserving)
    total_holdings = 0
    merged = 0
    provisional = 0

    try:
        # 1. job.started — MERGE the firm Company + register it as `onboarding` ----------------
        yield emit(
            EventType.JOB_STARTED,
            firm=name,
            cik=cik,
            lei=lei,
            series=[s.get("series_id") for s in series],
        )
        store.run(
            "MERGE (firm:Company {name: $n}) "
            "SET firm.cik = coalesce($cik, firm.cik), firm.lei = coalesce($lei, firm.lei)",
            n=name,
            cik=cik,
            lei=lei,
        )
        firms_store.upsert_firm(
            conn, name=name, cik=cik, lei=lei, status="onboarding", source="edgar"
        )

        # 2. per series — fetch → parse → write Fund/HOLDS → link MANAGED_BY -------------------
        from . import discovery

        for s in series:
            series_id = s.get("series_id")
            if not series_id:
                continue
            xml = discovery.fetch_series_nport_xml(series_id, settings=settings)
            if not xml:
                # Soft skip (not a hard failure): no NPORT-P filing resolved for this series.
                yield emit(
                    EventType.PARSED,
                    series_id=series_id,
                    fund_name=s.get("name"),
                    holdings=0,
                    skipped=True,
                    reason="no NPORT-P filing found",
                )
                continue

            filing = nport.parse_nport(xml)
            yield emit(
                EventType.PARSED,
                series_id=filing.series_id,
                fund_name=filing.fund_name,
                holdings=len(filing.holdings),
            )

            nport.write(store, filing)
            store.run(
                "MATCH (f:Fund {name: $fn}), (firm:Company {name: $firmn}) "
                "MERGE (f)-[:MANAGED_BY]->(firm)",
                fn=filing.fund_name,
                firmn=name,
            )
            n_issuers = len({h.name for h in filing.holdings})
            yield emit(
                EventType.WRITTEN,
                fund_name=filing.fund_name,
                nodes=1 + n_issuers,
                edges=len(filing.holdings) + 1,  # HOLDS per position + the MANAGED_BY link
            )

            funds_written.append(
                {
                    "name": filing.fund_name,
                    "series_id": filing.series_id,
                    "cik": filing.cik,
                    "lei": filing.lei,
                }
            )
            total_holdings += len(filing.holdings)
            for h in filing.holdings:
                issuers.setdefault(h.name, h.ticker)

        # 3. resolve each distinct holding issuer onto the spine + enrich its Company node -----
        if resolver is None:
            from ..ingest.pipeline import _default_resolver
            from ..providers.embeddings import get_embedder

            resolver = _default_resolver(settings, get_embedder(settings))

        for issuer_name, ticker in issuers.items():
            res = resolver.resolve(issuer_name, ticker=ticker, enrich_lei=False)
            if getattr(res, "resolved", False):
                merged += 1
                store.run(
                    "MATCH (co:Company {name: $n}) "
                    "SET co.cik = coalesce($cik, co.cik), "
                    "    co.lei = coalesce($lei, co.lei), "
                    "    co.ticker = coalesce($ticker, co.ticker)",
                    n=issuer_name,
                    cik=getattr(res, "cik", None),
                    lei=getattr(res, "lei", None),
                    ticker=getattr(res, "ticker", None),
                )
            else:
                provisional += 1
        yield emit(EventType.RESOLVED, merged=merged, provisional=provisional)

        # 4. flip the registry to `ready` (with funds/identifiers) and ACTIVATE the new firm ---
        identifiers = {
            "cik": cik,
            "lei": lei,
            "series": [{"series_id": f["series_id"], "name": f["name"]} for f in funds_written],
        }
        firm_id = firms_store.upsert_firm(
            conn,
            name=name,
            cik=cik,
            lei=lei,
            status="ready",
            source="edgar",
            identifiers=identifiers,
            funds=funds_written,
            activate=True,
        )
        completed: dict[str, Any] = {
            "ok": True,
            "firm_id": firm_id,
            "firm": name,
            "funds": len(funds_written),
            "fund_names": [f["name"] for f in funds_written],
            "holdings": total_holdings,
            "merged": merged,
            "provisional": provisional,
        }

        # 5. best-effort semantic enrichment — forwarded as a final "enriching" stage --------------
        # The firm is already structurally onboarded (status="ready" above), so enrichment must never
        # fail the onboarding: its whole loop is guarded, a failure emits one non-fatal error, and we
        # still emit the single terminal job.completed. enrich_firm's own lifecycle events are folded
        # away (its job.started dropped, its job.completed captured into `completed["enrichment"]`) so
        # the onboarding stream keeps exactly one job.started + one job.completed.
        if enrich:
            enrichment: dict[str, Any] = {
                "ran": False,
                "enriched": 0,
                "failed": 0,
                "funds_enriched": 0,
                "funds_failed": 0,
                "holdings_total": 0,
                "funds_total": 0,
                "risk_factors_added": 0,
            }
            try:
                for ev in enrich_firm(
                    name, settings=settings, store=store, provider=provider, conn=None
                ):
                    if ev.event == EventType.JOB_STARTED:
                        continue  # onboarding owns the single lifecycle job.started
                    if ev.event == EventType.JOB_COMPLETED:
                        enrichment = {
                            "ran": True,
                            "enriched": ev.data.get("enriched", 0),
                            "failed": ev.data.get("failed", 0),
                            "funds_enriched": ev.data.get("funds_enriched", 0),
                            "funds_failed": ev.data.get("funds_failed", 0),
                            "holdings_total": ev.data.get("holdings_total", 0),
                            "funds_total": ev.data.get("funds_total", 0),
                            "risk_factors_added": ev.data.get("risk_factors_added", 0),
                        }
                        continue
                    # forward every middle event (parsed/chunked/extracted/resolved/written/error)
                    yield emit(ev.event, **{**ev.data, "stage": "enriching"})
            except Exception as exc:  # noqa: BLE001 - enrichment is best-effort, never fatal here
                enrichment = {**enrichment, "ran": False, "error": str(exc)[:400]}
                yield emit(
                    EventType.ERROR,
                    where="onboard_firm:enrich",
                    message=str(exc)[:400],
                    fatal=False,
                    stage="enriching",
                )
            completed["enrichment"] = enrichment

        # 6. single terminal job.completed (structural counts + any enrichment counts) -------------
        yield emit(EventType.JOB_COMPLETED, **completed)

    except Exception as exc:  # noqa: BLE001 - surface on the stream, mark the registry failed
        try:
            firms_store.upsert_firm(
                conn, name=name, cik=cik, lei=lei, status="failed", source="edgar"
            )
        except Exception:  # noqa: BLE001 - best-effort; never mask the original failure
            pass
        yield emit(EventType.ERROR, message=str(exc)[:400], where="onboard_firm")
        raise
    finally:
        if own_conn and conn is not None:
            conn.close()
        if own_store:
            store.close()
