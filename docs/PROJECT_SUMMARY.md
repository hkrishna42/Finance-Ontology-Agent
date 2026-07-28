# Project Summary

A snapshot of what the Firm Ontology Platform is and its current state. See
[HANDOVER.md](HANDOVER.md) to continue the work.

## What it is

A local proof-of-concept that builds a **provenance-carrying knowledge graph** from SEC filings and
uses that ontology for three analytics tasks (Risk Lens, Change Impact, Regulatory Reporting) plus a
graph-grounded analyst chat. Originally scoped to one client, it was generalized so the **firm is a
runtime variable**: it ships an anonymized demo and lets a user onboard any real firm by name.

- **Repo:** https://github.com/hkrishna42/Finance-Ontology-Agent (public, `main`)
- **Stack:** FastAPI + SSE · React/Vite · Neo4j 5 (graph + 384-d vector) · SQLite · Claude behind an
  `LLMProvider` abstraction · Docker Compose · `uv`
- **Ontology:** 14 entities / 21 relations (9 / 16 extractable), 384-d embeddings — single source of
  truth in `api/ontology/schema.py`

## Current status: ✅ built, verified, published

| Area | Status |
|---|---|
| Anonymization (no real client identity) | ✅ 0 leaks in tree + live graph |
| Firm registry + active-firm selection + N-fund apps | ✅ committed, QA-verified |
| Firm onboarding (EDGAR/GLEIF search → live NPORT) | ✅ **live-verified** (Vanguard Explorer, 719 holdings) |
| Opt-in ontology enrichment | ✅ built + offline-tested (FakeProvider) |
| Containerization (images, compose, snapshot) | ✅ **CI multi-arch build green** |
| Offline gate (ruff + pytest) | ✅ 361 passed / 59 skipped |
| GitHub Actions CI | ✅ all jobs green (gate + compose config + amd64/arm64 image builds) |
| Published to public repo (clean history) | ✅ no commit contains the real client name |
| **Local `make bootstrap` run** | ⏳ blocked on this machine's Docker daemon (needs a reboot) |
| **Full-mode (real Claude) live smoke** | ⏭️ skipped by choice (no credit spend) |

## How it was built

Delivered in four phases, each built the harness-engineering way — an **orchestrator** coordinating
**parallel developer agents on a frozen contract** + an **adversarial QA gate** that fed bugs back and
re-verified until green:

- **Phase 0 — Anonymize.** The real client identity → "Demo Investment Management" + fictional
  LEIs/CIKs/tickers across ~45 files; snapshot regenerated. (4 parallel agents + QA.)
- **Phase 1 — Registry.** SQLite `firms` table, `/firms` CRUD + select, N-fund generalization of the
  apps, header firm selector. (4 parallel agents + QA.)
- **Phase 2 — Onboarding.** EDGAR/GLEIF discovery, the onboarding orchestrator (SSE), the Add-firm UI.
  (3 parallel agents + a **live** onboard QA.)
- **Phase 3 — Enrich.** Opt-in LLM semantic enrichment reusing the ingest pipeline. (1 agent + QA.)

The QA loop caught and fixed real defects, e.g. a sibling-series NPORT mismatch (would have corrupted
onboarding) and two CI failures (onboarding tests needed the `ingest` extra; the full-overlay
`env_file` had to be optional).

## Verification evidence

- **Offline gate:** `ruff` clean, 361 passed / 59 skipped (FakeProvider + HashEmbedder, no key/DB).
- **DB-backed:** 370 passed against a live Neo4j on the anonymized seed.
- **Live onboarding:** searched "Vanguard" → onboarded Explorer Fund → 719 real holdings written →
  selectable + scoped → deleted → demo re-activated.
- **CI (GitHub Actions):** offline gate + compose config + **multi-arch (amd64 + arm64) image builds**
  all green — this independently validates the container build.
- **Zero-leak scans** (working tree + committed tree + live graph) before publishing.

## Not done (by design or environment)

- **Local containerized run** (`make bootstrap`) — the only unrun acceptance; blocked on this Mac's
  Docker daemon being unable to pull images (a wedged Docker Desktop internal proxy; fix is a reboot /
  Docker reset). The images themselves are proven to build via CI.
- **Full-mode real-Claude smoke** — intentionally skipped to avoid spending credits; the LLM paths are
  proven offline via FakeProvider, and structural onboarding needs no key.

## Repo layout (top level)

```
api/            backend (ontology SSOT, providers, stores, modules, ingest, firms, onboarding, …)
web/            React + Vite frontend (firm selector + Add-firm modal + panels)
docker/         api.Dockerfile · web.Dockerfile · nginx.conf · api-entrypoint.sh
docs/           this documentation set
corpus/snapshot/ committed anonymized demo graph (restored on first boot)
fixtures/       FakeProvider outputs, mini filings, golden files, seed graph
scripts/        make_snapshot · demo_check · qa_check · load_seed · apply_ddl
docker-compose*.yml   base (stub) · .full.yml (real Claude) · .prebuilt.yml (GHCR)
.github/workflows/    ci.yml · release.yml
```
