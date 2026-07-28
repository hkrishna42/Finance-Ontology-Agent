# Handover — start here

For the next session/developer picking this up. Read [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) for the
state, [ARCHITECTURE.md](ARCHITECTURE.md) for the design, [FEATURES.md](FEATURES.md) for the surface.

## TL;DR

The platform is **built, verified, and published** (public repo, CI green). It was generalized from a
single-client demo into a **firm-as-a-runtime-variable** system: ships an anonymized demo, onboards any
real firm by name via SEC EDGAR/GLEIF. The only unfinished acceptance is running the **containers
locally**, which is blocked on this machine's Docker daemon (a host issue, not code).

## Repo & branches

- **Remote:** `origin` → https://github.com/hkrishna42/Finance-Ontology-Agent (public).
- **`main`** = the clean, anonymized public history (3 commits: app · infra · CI fix). **No commit
  contains the real client name** — keep it that way.
- **`dev-history-full`** (local only, not pushed) = the full granular dev history, including the
  pre-anonymization commits. Don't push this to the public remote.
- Working tree is clean and matches `main`.

## Run it

```bash
# Offline gate (no Docker/key/DB needed) — should be green
make ci                     # ruff + pytest → 361 passed / 59 skipped

# Full local stack (needs a working Docker daemon — see the blocker below)
make bootstrap              # stub: offline, loads the committed demo snapshot
make bootstrap MODE=full    # real Claude: needs .env with ANTHROPIC_API_KEY
# → open http://localhost:5173
```

**Host dev loop (no Docker build)** — what this session used to iterate:
```bash
docker compose up -d neo4j                                   # cached image; the daemon can RUN it
NEO4J_URI=bolt://localhost:7687 PROVIDER_MODE=stub EMBED_BACKEND=hash \
  uv run python -m api.snapshot --restore                    # load the demo graph
NEO4J_URI=bolt://localhost:7687 PROVIDER_MODE=stub EMBED_BACKEND=hash \
  uv run uvicorn api.main:app --port 8000                    # API
cd web && npm run dev -- --port 5173                         # web (proxies to :8000)
```

## ⚠️ Known blockers & gotchas

1. **Docker daemon can't pull images (host issue).** On this Mac the daemon hangs pulling from any
   registry via its internal proxy (`http.docker.internal:3128`) while the host's own network is fine.
   Restarts didn't fix it. **Fix: reboot macOS** (or Docker Desktop → Troubleshoot → Reset), then
   `docker pull hello-world` to confirm, then `make bootstrap`. The images are proven to build via CI.
2. **Onboarded holdings resolve as *provisional*.** The SEC/GLEIF resolution spine
   (`fixtures/resolution/`) only contains the ~13 demo issuers, so a real firm's holdings mostly don't
   resolve to CIK/LEI and land in the provisional queue. The nodes are still created correctly. To
   improve: resolve issuers live against EDGAR/GLEIF during onboarding (extend `api/resolution/`).
3. **Money-market fund series have no NPORT-P** (they file N-MFP). `discovery.fetch_series_nport_xml`
   returns `None` for those and the onboarder emits a "skipped" event — expected. A sibling-series
   guard rejects a mismatched `seriesId` (a real bug that was caught in QA).
4. **`DELETE /firms/{id}` leaves orphan holding nodes.** It removes the firm + its Fund nodes but not
   the (potentially shared) issuer Company nodes, so isolated holdings can accumulate. Minor; a
   `make reset` or snapshot re-restore clears everything. A cleanup pass could delete now-orphaned,
   non-shared issuers.
5. **Full mode needs API credit on the same org as the key.** Keep any real-Claude runs tiny.

## Suggested next steps (roughly prioritized)

1. **Finish the local container acceptance** — reboot Docker, `make bootstrap`, then run a fresh-clone
   check: `git clone <repo> /tmp/fop-verify && cd /tmp/fop-verify && make bootstrap` → UI + `bash
   scripts/demo_check.sh`.
2. **Cut a release** — `git tag v0.1.0 && git push origin v0.1.0` triggers `release.yml` to publish
   multi-arch images to GHCR + attach the prebuilt compose + snapshot, so others run it without
   building (`docker compose -f docker-compose.yml -f docker-compose.prebuilt.yml up`).
3. **Full-mode live validation** (optional, spends credit) — a tiny `/query` + a bounded
   `/firms/{id}/enrich` on one holding to prove the real-Claude paths end-to-end.
4. **Live issuer resolution during onboarding** (see gotcha 2) — the biggest quality win for onboarded
   firms; would make Risk Lens supply-chain analysis meaningful beyond the demo.
5. **Enrichment depth** — also enrich each fund's prospectus (485BPOS principal-risks), not just top
   holdings' 10-Ks; wire an "Enrich" button in the UI.
6. **Onboarding polish** — surface skipped/failed series in the Add-firm modal; a "clear demo firm"
   affordance; persist onboarded-firm identifiers (LEI/tickers) onto the graph nodes.
7. **Orphan cleanup on firm delete** (gotcha 4).

## How this was built (continue the pattern)

Work was done in **phases**, each: (1) the orchestrator freezes a **contract** (interfaces/shapes) in a
shared file; (2) **parallel developer agents** implement disjoint file-ownership slices against it;
(3) an **adversarial QA agent/gate** runs ruff + offline pytest + DB-backed + endpoint/adversarial
checks and feeds findings back; (4) fix → re-verify → commit. This kept ~90% of the parallel work
conflict-free and caught real bugs. The Phase 1/2 contracts were written to the session scratchpad
(not committed); re-create equivalents for new phases.

Guardrails that must stay green:
- `make ci` (ruff + offline pytest) — the offline gate.
- Case-insensitive `git grep` of the tree for the **old client/firm name and its identifiers**
  (LEIs/CIKs/series/tickers) → **must be 0** before any push to the public repo.
- `tests/test_snapshot.py` — the embedder/snapshot drift alarm; run `make snapshot` after editing
  `fixtures/seed_graph.cypher`.
- CI (`.github/workflows/ci.yml`) — offline gate + compose config + multi-arch image builds.

## Key files to know

| Need | File |
|---|---|
| Ontology (SSOT) | `api/ontology/schema.py` |
| Demo snapshot generate/restore | `api/snapshot.py`, `scripts/make_snapshot.sh` |
| Firm registry | `api/firms/store.py` · `graph_ops.py` · `routes.py` |
| Onboarding | `api/onboarding/discovery.py` · `pipeline.py` · `enrich.py` · `routes.py` |
| NPORT parser (reused) | `api/l2/nport.py` |
| App scoping to active firm | `api/modules/risk_routes.py`, `reg_routes.py` |
| Firm selector + Add-firm UI | `web/src/components/FirmSelector.tsx` · `AddFirmModal.tsx` |
| Containers | `docker/`, `docker-compose*.yml`, `Makefile`, `bootstrap.sh` |
| Approved plan (this session) | `~/.claude/plans/eager-bubbling-russell.md` (local, not in repo) |
