"""Seed-mirrored, in-memory fixtures for the Apps workstream's OFFLINE unit tests.

This mirrors `fixtures/seed_graph.cypher` as plain Python so the M5/M6/M7 pure functions can be
unit-tested with zero Neo4j / zero network (CI-safe). The `test_*_neo4j.py` integration tests
assert the SAME numbers against a live seeded graph, so any drift between this mirror and the
seed is caught there. Not named `test_*`, so pytest does not collect it as a test module.
"""

from __future__ import annotations

from api.modules.risk_lens import FOCUSED_FUND, GROWTH_FUND, GraphSlice

# --- Companies ------------------------------------------------------------------------------
COMPANIES: dict[str, tuple[str, str]] = {
    "NVIDIA": ("NVDA", "0001045810"),
    "Microsoft": ("MSFT", "0000789019"),
    "Apple": ("AAPL", "0000320193"),
    "Amazon": ("AMZN", "0001018724"),
    "Meta Platforms": ("META", "0001326801"),
    "Broadcom": ("AVGO", "0001730168"),
    "Eli Lilly": ("LLY", "0000059478"),
    "Advanced Micro Devices": ("AMD", "0000002488"),
    "Taiwan Semiconductor Manufacturing": ("TSM", "0001046179"),
    "ASML Holding": ("ASML", "0000937966"),
}

# --- HOLDS (name, weight_pct, value_usd) ----------------------------------------------------
GROWTH_HOLDINGS = [
    ("NVIDIA", 11.5, 920000000), ("Microsoft", 9.0, 720000000), ("Apple", 7.5, 600000000),
    ("Amazon", 7.0, 560000000), ("Meta Platforms", 5.5, 440000000), ("Broadcom", 5.0, 400000000),
    ("Eli Lilly", 4.5, 360000000), ("Advanced Micro Devices", 3.5, 280000000),
    ("Taiwan Semiconductor Manufacturing", 2.5, 200000000),
]
FOCUSED_HOLDINGS = [
    ("NVIDIA", 16.0, 480000000), ("Microsoft", 12.0, 360000000), ("Apple", 9.0, 270000000),
    ("Amazon", 8.5, 255000000), ("Broadcom", 7.5, 225000000), ("Meta Platforms", 6.5, 195000000),
    ("Eli Lilly", 6.0, 180000000), ("Advanced Micro Devices", 5.0, 150000000),
]

# --- SUPPLIES_TO (all critical in the seed) -------------------------------------------------
SUPPLIES = [
    {"src": "NVIDIA", "dst": "Taiwan Semiconductor Manufacturing", "criticality": "critical",
     "component": "advanced GPU foundry / CoWoS packaging", "doc_id": "nvda_10k",
     "chunk_id": "nvda_10k_c1", "confidence": 0.95,
     "span": "We depend on a limited number of foundries, principally Taiwan Semiconductor "
             "Manufacturing Company (TSMC), for the fabrication of our GPUs"},
    {"src": "Advanced Micro Devices", "dst": "Taiwan Semiconductor Manufacturing",
     "criticality": "critical", "component": "leading-edge foundry", "doc_id": "amd_10k",
     "chunk_id": "nvda_10k_c1", "confidence": 0.95,
     "span": "AMD relies on TSMC for the manufacture of its leading-edge processors"},
    {"src": "Apple", "dst": "Taiwan Semiconductor Manufacturing", "criticality": "critical",
     "component": "application processor foundry", "doc_id": "amd_10k",
     "chunk_id": "nvda_10k_c1", "confidence": 0.95,
     "span": "Apple sources its custom silicon from TSMC"},
    {"src": "Broadcom", "dst": "Taiwan Semiconductor Manufacturing", "criticality": "critical",
     "component": "ASIC foundry", "doc_id": "amd_10k", "chunk_id": "nvda_10k_c1",
     "confidence": 0.95, "span": "Broadcom depends on TSMC for advanced ASICs"},
    {"src": "Taiwan Semiconductor Manufacturing", "dst": "ASML Holding", "criticality": "critical",
     "component": "EUV lithography systems", "doc_id": "tsmc_20f", "chunk_id": "tsmc_20f_c1",
     "confidence": 0.95,
     "span": "Our leading-edge manufacturing depends on extreme ultraviolet (EUV) lithography "
             "systems supplied by ASML"},
]

# --- RiskFactor title -> category -----------------------------------------------------------
RISKFACTORS = {
    "Advanced foundry / supply concentration": "supply_chain",
    "Customer concentration": "customer_concentration",
    "Export controls and geopolitics": "geopolitical",
    "AI accelerator competition": "technology",
    "Antitrust and regulatory scrutiny": "regulatory",
}

# --- EXPOSED_TO (company, risk_title, severity_language) ------------------------------------
EXPOSED = [
    ("NVIDIA", "Advanced foundry / supply concentration",
     "substantial dependence on a limited number of foundries"),
    ("NVIDIA", "Customer concentration",
     "a limited number of customers account for a substantial portion of revenue"),
    ("NVIDIA", "Export controls and geopolitics", "export restrictions may materially harm our business"),
    ("NVIDIA", "AI accelerator competition", "the market is highly competitive"),
    ("Advanced Micro Devices", "Advanced foundry / supply concentration", "we rely on TSMC"),
    ("Advanced Micro Devices", "AI accelerator competition", "intense competition"),
    ("Apple", "Advanced foundry / supply concentration", "concentration of manufacturing"),
    ("Broadcom", "Advanced foundry / supply concentration", "dependence on third-party foundries"),
    ("Taiwan Semiconductor Manufacturing", "Advanced foundry / supply concentration",
     "dependence on ASML for EUV"),
    ("Taiwan Semiconductor Manufacturing", "Export controls and geopolitics", "cross-strait tensions"),
    ("Microsoft", "Antitrust and regulatory scrutiny", "increasing regulatory scrutiny"),
    ("Apple", "Antitrust and regulatory scrutiny", "regulatory investigations"),
]

# --- Provenance: chunk -> mentioned companies, chunk -> doc, doc -> filing_date --------------
MENTIONS = {
    "nvda_10k_c1": ["NVIDIA", "Taiwan Semiconductor Manufacturing"],
    "tsmc_20f_c1": ["ASML Holding"],
    "internal_note_c1": ["NVIDIA"],
}
CHUNK_DOC = {"nvda_10k_c1": "nvda_10k", "tsmc_20f_c1": "tsmc_20f", "internal_note_c1": "internal_note"}
DOC_DATE = {
    "nvda_10k": "2026-02-26", "amd_10k": "2026-02-04", "tsmc_20f": "2026-04-16",
    "internal_note": "2026-07-20",
}

FUND_SERIES = {GROWTH_FUND: "S000090001", FOCUSED_FUND: "S000090002"}
HOLDINGS_BY_FUND = {GROWTH_FUND: GROWTH_HOLDINGS, FOCUSED_FUND: FOCUSED_HOLDINGS}

# DisclosureSection COVERS: section_key -> (form_ref, covered risk title)
COVERS = {
    "S000090001:485BPOS|tech": ("S000090001:485BPOS", "AI accelerator competition"),
    "S000090001:485BPOS|conc": ("S000090001:485BPOS", "Customer concentration"),
    "S000090001:485BPOS|reg": ("S000090001:485BPOS", "Antitrust and regulatory scrutiny"),
    "S000090002:485BPOS|tech": ("S000090002:485BPOS", "AI accelerator competition"),
    "S000090002:485BPOS|conc": ("S000090002:485BPOS", "Customer concentration"),
}
SECTION_TITLES = {
    "S000090001:485BPOS|tech": "Technology and Growth Investing Risk",
    "S000090001:485BPOS|conc": "Company and Sector Concentration Risk",
    "S000090001:485BPOS|reg": "Regulatory Risk",
    "S000090002:485BPOS|tech": "Technology and Growth Investing Risk",
    "S000090002:485BPOS|conc": "Non-Diversification and Concentration Risk",
}
SECTION_ITEMS = {
    "S000090001:485BPOS|tech": "principal_risks/technology",
    "S000090001:485BPOS|conc": "principal_risks/concentration",
    "S000090001:485BPOS|reg": "principal_risks/regulatory",
    "S000090002:485BPOS|tech": "principal_risks/technology",
    "S000090002:485BPOS|conc": "principal_risks/concentration",
}


def exposed_rows(fund: str) -> list[dict]:
    """M7(b) inputs: held companies EXPOSED_TO risk factors (mirrors EXPOSED_CYPHER)."""
    return _exposure_rows(fund)


def covered_rows() -> list[dict]:
    """M7(b) inputs: all DisclosureSection COVERS rows (mirrors COVERED_CYPHER)."""
    return [
        {"section_key": k, "form_ref": fr, "section_title": SECTION_TITLES[k],
         "risk_title": rt, "category": RISKFACTORS[rt]}
        for k, (fr, rt) in COVERS.items()
    ]


def holdings_rows(fund: str) -> list[dict]:
    """M7(a) inputs: HOLDS rows for a fund (adds no `shares` — absent in the seed)."""
    return _holdings_rows(fund)


def _holdings_rows(fund: str) -> list[dict]:
    rows = []
    for name, w, v in HOLDINGS_BY_FUND[fund]:
        ticker, cik = COMPANIES[name]
        rows.append({"company": name, "ticker": ticker, "cik": cik, "weight_pct": w,
                     "value_usd": v, "as_of": "2026-05-31", "source": "NPORT-P:seed"})
    rows.sort(key=lambda r: -r["weight_pct"])
    return rows


def _exposure_rows(fund: str) -> list[dict]:
    weights = {n: w for n, w, _ in HOLDINGS_BY_FUND[fund]}
    rows = []
    for company, title, sev in EXPOSED:
        if company in weights:
            rows.append({"company": company, "weight_pct": weights[company],
                         "risk_title": title, "category": RISKFACTORS[title],
                         "severity": sev, "doc_id": "nvda_10k", "chunk_id": None})
    return rows


def _provenance_rows(fund: str) -> list[dict]:
    weights = {n: w for n, w, _ in HOLDINGS_BY_FUND[fund]}
    mentioned: dict[str, list[str]] = {}
    for chunk, companies in MENTIONS.items():
        for co in companies:
            mentioned.setdefault(co, []).append(chunk)
    rows = []
    for name, w, _ in HOLDINGS_BY_FUND[fund]:
        chunks = sorted(mentioned.get(name, []))
        docs = sorted({CHUNK_DOC[c] for c in chunks})
        dates = sorted({DOC_DATE[d] for d in docs})
        rows.append({"company": name, "weight_pct": w, "chunks": chunks,
                     "docs": docs, "filing_dates": dates})
    _ = weights
    return rows


def slice_for(fund: str) -> GraphSlice:
    return GraphSlice(
        fund=fund,
        holdings=_holdings_rows(fund),
        supplies=[dict(s) for s in SUPPLIES],
        exposures=_exposure_rows(fund),
        provenance=_provenance_rows(fund),
    )


def growth_slice() -> GraphSlice:
    return slice_for(GROWTH_FUND)


def focused_slice() -> GraphSlice:
    return slice_for(FOCUSED_FUND)


def seed_runner():
    """A fake `.run(query, **params)` that maps the modules' exact Cypher to seed-mirrored rows.

    Matching is by *exact query equality* against the module constants, so a query edit fails the
    offline test loudly rather than silently returning stale rows.
    """
    from api.modules import risk_lens as rl
    from api.modules.reg_reports import coverage as cov
    from api.modules.reg_reports import thirteen_f as tf

    class _Runner:
        def run(self, query: str, **params):
            fund = params.get("fund")
            if query == rl.CYPHER["holdings"]:
                return _holdings_rows(fund)
            if query == rl.CYPHER["supplies"]:
                return [dict(s) for s in SUPPLIES]
            if query == rl.CYPHER["exposures"]:
                return _exposure_rows(fund)
            if query == rl.CYPHER["provenance"]:
                return _provenance_rows(fund)
            if query == cov.EXPOSED_CYPHER:
                return _exposure_rows(fund)
            if query == cov.COVERED_CYPHER:
                return covered_rows()
            if query == cov.SERIES_CYPHER:
                return [{"series_id": FUND_SERIES[fund]}]
            if query == tf.HOLDINGS_CYPHER:
                return _holdings_rows(fund)
            raise KeyError(f"seed_runner: unmapped query:\n{query}")

    return _Runner()


def seed_resolver():
    """Build an in-memory DictResolver (M6) mirroring the seed graph's HOLDS/SUPPLIES/COVERS."""
    from api.modules.change_impact import DictResolver

    holds = [
        (fund, name, w)
        for fund, rows in HOLDINGS_BY_FUND.items()
        for (name, w, _v) in rows
    ]
    supplies = [(s["src"], s["dst"]) for s in SUPPLIES]  # (dependent, supplier)
    covers = [(key, form_ref, title) for key, (form_ref, title) in COVERS.items()]
    section_meta = {
        key: {"item": SECTION_ITEMS[key], "title": SECTION_TITLES[key]} for key in COVERS
    }
    return DictResolver(holds=holds, supplies=supplies, covers=covers,
                        series=dict(FUND_SERIES), section_meta=section_meta)
