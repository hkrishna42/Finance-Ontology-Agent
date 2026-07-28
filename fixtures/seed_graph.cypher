// Seed graph — the shared, ontology-consistent demo subgraph.
// Loaded by scripts/load_seed.py (which also computes + sets :Chunk embeddings via get_embedder).
// Supports every hero query: shared critical supplier (NVDA/AMD/AAPL -> TSMC), second-order
// chokepoint (TSMC -> ASML), cross-fund concentration/HHI, the disclosure-coverage GAP
// (supply_chain exposure with no matching prospectus section), a board interlock, and the wall.
// Statements are ";"-terminated; no ";" appears inside string literals.

// --- Firm + funds -------------------------------------------------------------------------
MERGE (firm:Company {name: 'Demo Investment Management'}) SET firm.country = 'US';
MERGE (gf:Fund {name: 'Demo Growth Fund'})
  SET gf.series_id = 'S000090001', gf.cik = '0009000001', gf.lei = 'DEMOGROWTHFUND000001';
MERGE (ff:Fund {name: 'Demo Focused Growth Fund'})
  SET ff.series_id = 'S000090002', ff.cik = '0009000002', ff.lei = 'DEMOFOCUSEDGRWTH0001';
MATCH (gf:Fund {name: 'Demo Growth Fund'}), (firm:Company {name: 'Demo Investment Management'})
  MERGE (gf)-[:MANAGED_BY]->(firm);
MATCH (ff:Fund {name: 'Demo Focused Growth Fund'}), (firm:Company {name: 'Demo Investment Management'})
  MERGE (ff)-[:MANAGED_BY]->(firm);

// --- Issuers (holdings + supply chain) ----------------------------------------------------
UNWIND [
  {name: 'NVIDIA', ticker: 'NVDA', cik: '0001045810'},
  {name: 'Microsoft', ticker: 'MSFT', cik: '0000789019'},
  {name: 'Apple', ticker: 'AAPL', cik: '0000320193'},
  {name: 'Amazon', ticker: 'AMZN', cik: '0001018724'},
  {name: 'Meta Platforms', ticker: 'META', cik: '0001326801'},
  {name: 'Broadcom', ticker: 'AVGO', cik: '0001730168'},
  {name: 'Eli Lilly', ticker: 'LLY', cik: '0000059478'},
  {name: 'Advanced Micro Devices', ticker: 'AMD', cik: '0000002488'},
  {name: 'Taiwan Semiconductor Manufacturing', ticker: 'TSM', cik: '0001046179'},
  {name: 'ASML Holding', ticker: 'ASML', cik: '0000937966'}
] AS c
MERGE (co:Company {name: c.name}) SET co.ticker = c.ticker, co.cik = c.cik, co.country = 'US';

// --- HOLDS: Growth fund (diversified) -----------------------------------------------------
UNWIND [
  {n: 'NVIDIA', w: 11.5, v: 920000000}, {n: 'Microsoft', w: 9.0, v: 720000000},
  {n: 'Apple', w: 7.5, v: 600000000}, {n: 'Amazon', w: 7.0, v: 560000000},
  {n: 'Meta Platforms', w: 5.5, v: 440000000}, {n: 'Broadcom', w: 5.0, v: 400000000},
  {n: 'Eli Lilly', w: 4.5, v: 360000000}, {n: 'Advanced Micro Devices', w: 3.5, v: 280000000},
  {n: 'Taiwan Semiconductor Manufacturing', w: 2.5, v: 200000000}
] AS h
MATCH (gf:Fund {name: 'Demo Growth Fund'}), (co:Company {name: h.n})
MERGE (gf)-[r:HOLDS]->(co)
SET r.weight_pct = h.w, r.value_usd = h.v, r.as_of = '2026-05-31', r.source = 'NPORT-P:seed';

// --- HOLDS: Focused fund (concentrated, higher weights) -----------------------------------
UNWIND [
  {n: 'NVIDIA', w: 16.0, v: 480000000}, {n: 'Microsoft', w: 12.0, v: 360000000},
  {n: 'Apple', w: 9.0, v: 270000000}, {n: 'Amazon', w: 8.5, v: 255000000},
  {n: 'Broadcom', w: 7.5, v: 225000000}, {n: 'Meta Platforms', w: 6.5, v: 195000000},
  {n: 'Eli Lilly', w: 6.0, v: 180000000}, {n: 'Advanced Micro Devices', w: 5.0, v: 150000000}
] AS h
MATCH (ff:Fund {name: 'Demo Focused Growth Fund'}), (co:Company {name: h.n})
MERGE (ff)-[r:HOLDS]->(co)
SET r.weight_pct = h.w, r.value_usd = h.v, r.as_of = '2026-05-31', r.source = 'NPORT-P:seed';

// --- Provenance sources -------------------------------------------------------------------
MERGE (d1:Document {doc_id: 'nvda_10k'})
  SET d1.doc_type = '10-K', d1.accession_no = 'seed', d1.sensitivity = 'public',
      d1.title = 'NVIDIA FY2025 10-K', d1.filing_date = '2026-02-26';
MERGE (d2:Document {doc_id: 'amd_10k'})
  SET d2.doc_type = '10-K', d2.accession_no = 'seed', d2.sensitivity = 'public',
      d2.title = 'AMD FY2024 10-K', d2.filing_date = '2026-02-04';
MERGE (d3:Document {doc_id: 'tsmc_20f'})
  SET d3.doc_type = '20-F', d3.accession_no = 'seed', d3.sensitivity = 'public',
      d3.title = 'TSMC FY2024 20-F', d3.filing_date = '2026-04-16';
MERGE (dn:Document {doc_id: 'internal_note'})
  SET dn.doc_type = 'analyst_note', dn.accession_no = 'seed', dn.sensitivity = 'internal',
      dn.title = 'NVDA supply chain follow-up (SYNTHETIC)', dn.filing_date = '2026-07-20';
MERGE (c1:Chunk {chunk_id: 'nvda_10k_c1'})
  SET c1.doc_id = 'nvda_10k', c1.page = '22',
      c1.text = 'We depend on a limited number of foundries, principally Taiwan Semiconductor Manufacturing Company (TSMC), for the fabrication of our GPUs, and on advanced packaging capacity that is currently constrained.';
MERGE (c2:Chunk {chunk_id: 'nvda_10k_c2'})
  SET c2.doc_id = 'nvda_10k', c2.page = '24',
      c2.text = 'A limited number of hyperscale customers account for a substantial portion of our data center revenue.';
MERGE (c3:Chunk {chunk_id: 'tsmc_20f_c1'})
  SET c3.doc_id = 'tsmc_20f', c3.page = '15',
      c3.text = 'Our leading-edge manufacturing depends on extreme ultraviolet (EUV) lithography systems supplied by ASML.';
MERGE (cn:Chunk {chunk_id: 'internal_note_c1'})
  SET cn.doc_id = 'internal_note', cn.page = '1', cn.sensitivity = 'internal',
      cn.text = 'One hyperscale customer is negotiating multi-year committed capacity that would push NVDA concentration above the level disclosed in the 10-K.';

// --- SUPPLIES_TO (critical foundry dependencies + second-order chokepoint) -----------------
UNWIND [
  {s: 'NVIDIA', o: 'Taiwan Semiconductor Manufacturing', comp: 'advanced GPU foundry / CoWoS packaging', doc: 'nvda_10k', ch: 'nvda_10k_c1', span: 'We depend on a limited number of foundries, principally Taiwan Semiconductor Manufacturing Company (TSMC), for the fabrication of our GPUs'},
  {s: 'Advanced Micro Devices', o: 'Taiwan Semiconductor Manufacturing', comp: 'leading-edge foundry', doc: 'amd_10k', ch: 'nvda_10k_c1', span: 'AMD relies on TSMC for the manufacture of its leading-edge processors'},
  {s: 'Apple', o: 'Taiwan Semiconductor Manufacturing', comp: 'application processor foundry', doc: 'amd_10k', ch: 'nvda_10k_c1', span: 'Apple sources its custom silicon from TSMC'},
  {s: 'Broadcom', o: 'Taiwan Semiconductor Manufacturing', comp: 'ASIC foundry', doc: 'amd_10k', ch: 'nvda_10k_c1', span: 'Broadcom depends on TSMC for advanced ASICs'},
  {s: 'Taiwan Semiconductor Manufacturing', o: 'ASML Holding', comp: 'EUV lithography systems', doc: 'tsmc_20f', ch: 'tsmc_20f_c1', span: 'Our leading-edge manufacturing depends on extreme ultraviolet (EUV) lithography systems supplied by ASML'}
] AS e
MATCH (a:Company {name: e.s}), (b:Company {name: e.o})
MERGE (a)-[r:SUPPLIES_TO]->(b)
SET r.criticality = 'critical', r.component = e.comp, r.doc_id = e.doc, r.chunk_id = e.ch,
    r.span = e.span, r.confidence = 0.95, r.sensitivity = 'public', r.extractor_model = 'seed';

// --- CUSTOMER_OF / COMPETES_WITH ----------------------------------------------------------
UNWIND ['Microsoft', 'Meta Platforms', 'Amazon'] AS cust
MATCH (a:Company {name: cust}), (nv:Company {name: 'NVIDIA'})
MERGE (a)-[r:CUSTOMER_OF]->(nv) SET r.doc_id = 'nvda_10k', r.confidence = 0.9, r.sensitivity = 'public';
MATCH (nv:Company {name: 'NVIDIA'}), (amd:Company {name: 'Advanced Micro Devices'})
MERGE (nv)-[r:COMPETES_WITH]->(amd)
SET r.market = 'GPU / AI accelerators', r.doc_id = 'nvda_10k', r.confidence = 0.9, r.sensitivity = 'public';

// --- Risk factors + EXPOSED_TO (heatmap) --------------------------------------------------
UNWIND [
  {t: 'Advanced foundry / supply concentration', cat: 'supply_chain'},
  {t: 'Customer concentration', cat: 'customer_concentration'},
  {t: 'Export controls and geopolitics', cat: 'geopolitical'},
  {t: 'AI accelerator competition', cat: 'technology'},
  {t: 'Antitrust and regulatory scrutiny', cat: 'regulatory'}
] AS rf
MERGE (r:RiskFactor {title: rf.t}) SET r.category = rf.cat;

UNWIND [
  {c: 'NVIDIA', rf: 'Advanced foundry / supply concentration', sev: 'substantial dependence on a limited number of foundries'},
  {c: 'NVIDIA', rf: 'Customer concentration', sev: 'a limited number of customers account for a substantial portion of revenue'},
  {c: 'NVIDIA', rf: 'Export controls and geopolitics', sev: 'export restrictions may materially harm our business'},
  {c: 'NVIDIA', rf: 'AI accelerator competition', sev: 'the market is highly competitive'},
  {c: 'Advanced Micro Devices', rf: 'Advanced foundry / supply concentration', sev: 'we rely on TSMC'},
  {c: 'Advanced Micro Devices', rf: 'AI accelerator competition', sev: 'intense competition'},
  {c: 'Apple', rf: 'Advanced foundry / supply concentration', sev: 'concentration of manufacturing'},
  {c: 'Broadcom', rf: 'Advanced foundry / supply concentration', sev: 'dependence on third-party foundries'},
  {c: 'Taiwan Semiconductor Manufacturing', rf: 'Advanced foundry / supply concentration', sev: 'dependence on ASML for EUV'},
  {c: 'Taiwan Semiconductor Manufacturing', rf: 'Export controls and geopolitics', sev: 'cross-strait tensions'},
  {c: 'Microsoft', rf: 'Antitrust and regulatory scrutiny', sev: 'increasing regulatory scrutiny'},
  {c: 'Apple', rf: 'Antitrust and regulatory scrutiny', sev: 'regulatory investigations'}
] AS x
MATCH (co:Company {name: x.c}), (r:RiskFactor {title: x.rf})
MERGE (co)-[e:EXPOSED_TO]->(r)
// span (canonical provenance, PROVENANCE_FIELDS) == the verbatim severity_language hedge here.
SET e.severity_language = x.sev, e.span = x.sev, e.doc_id = 'nvda_10k',
    e.confidence = 0.9, e.sensitivity = 'public';

// --- Regulatory forms + SUBJECT_TO --------------------------------------------------------
UNWIND [{f: '13F', freq: 'quarterly'}, {f: 'N-PORT', freq: 'monthly'}, {f: '485BPOS', freq: 'annual'}] AS rf
MERGE (r:RegulatoryForm {form: rf.f}) SET r.frequency = rf.freq;
UNWIND ['Demo Growth Fund', 'Demo Focused Growth Fund'] AS fn
UNWIND ['13F', 'N-PORT', '485BPOS'] AS form
MATCH (f:Fund {name: fn}), (rf:RegulatoryForm {form: form}) MERGE (f)-[:SUBJECT_TO]->(rf);

// --- DisclosureSection + COVERS (deliberate coverage GAP on supply_chain) -------------------
// Growth fund prospectus covers technology, customer-concentration, regulatory, market — but
// NOT the supply_chain / foundry exposure. That gap is what the Reg Assistant flags.
UNWIND [
  {key: 'S000090001:485BPOS|tech', form: 'S000090001:485BPOS', item: 'principal_risks/technology', title: 'Technology and Growth Investing Risk', covers: 'AI accelerator competition'},
  {key: 'S000090001:485BPOS|conc', form: 'S000090001:485BPOS', item: 'principal_risks/concentration', title: 'Company and Sector Concentration Risk', covers: 'Customer concentration'},
  {key: 'S000090001:485BPOS|reg', form: 'S000090001:485BPOS', item: 'principal_risks/regulatory', title: 'Regulatory Risk', covers: 'Antitrust and regulatory scrutiny'},
  {key: 'S000090002:485BPOS|tech', form: 'S000090002:485BPOS', item: 'principal_risks/technology', title: 'Technology and Growth Investing Risk', covers: 'AI accelerator competition'},
  {key: 'S000090002:485BPOS|conc', form: 'S000090002:485BPOS', item: 'principal_risks/concentration', title: 'Non-Diversification and Concentration Risk', covers: 'Customer concentration'}
] AS ds
MERGE (s:DisclosureSection {key: ds.key}) SET s.form_ref = ds.form, s.item = ds.item, s.title = ds.title
WITH s, ds MATCH (r:RiskFactor {title: ds.covers}) MERGE (s)-[:COVERS]->(r);

// --- People (board interlock + alumni path) -----------------------------------------------
MERGE (p1:Person {name: 'Ada Interlock'});
MERGE (p2:Person {name: 'Ben Alumni'});
MATCH (p:Person {name: 'Ada Interlock'}), (nv:Company {name: 'NVIDIA'})
  MERGE (p)-[r:DIRECTOR_OF]->(nv) SET r.committee = 'Audit', r.doc_id = 'nvda_def14a', r.confidence = 0.95, r.sensitivity = 'public';
MATCH (p:Person {name: 'Ada Interlock'}), (ms:Company {name: 'Microsoft'})
  MERGE (p)-[r:DIRECTOR_OF]->(ms) SET r.committee = 'Compensation', r.doc_id = 'msft_def14a', r.confidence = 0.95, r.sensitivity = 'public';
MATCH (p:Person {name: 'Ben Alumni'}), (nv:Company {name: 'NVIDIA'})
  MERGE (p)-[r:OFFICER_OF]->(nv) SET r.title = 'SVP Operations', r.doc_id = 'nvda_def14a', r.confidence = 0.9, r.sensitivity = 'public';
MATCH (p:Person {name: 'Ben Alumni'}), (amd:Company {name: 'Advanced Micro Devices'})
  MERGE (p)-[r:FORMERLY_AT]->(amd) SET r.role = 'VP Engineering', r.years = '2012-2019', r.doc_id = 'nvda_def14a', r.confidence = 0.85, r.sensitivity = 'public';

// --- Metric observation -------------------------------------------------------------------
MERGE (m:MetricObservation {key: 'revenue|FY2025|GAAP'})
  SET m.metric = 'revenue', m.value = '130.5', m.unit = 'USD_bn', m.period = 'FY2025', m.basis = 'GAAP';
MATCH (nv:Company {name: 'NVIDIA'}), (m:MetricObservation {key: 'revenue|FY2025|GAAP'})
  MERGE (nv)-[r:REPORTED]->(m) SET r.doc_id = 'nvda_10k', r.confidence = 1.0, r.sensitivity = 'public';

// --- MENTIONS (chunk -> entity provenance, incl. the internal-note wall) --------------------
MATCH (c:Chunk {chunk_id: 'nvda_10k_c1'}), (nv:Company {name: 'NVIDIA'}) MERGE (c)-[:MENTIONS]->(nv);
MATCH (c:Chunk {chunk_id: 'nvda_10k_c1'}), (t:Company {name: 'Taiwan Semiconductor Manufacturing'}) MERGE (c)-[:MENTIONS]->(t);
MATCH (c:Chunk {chunk_id: 'tsmc_20f_c1'}), (a:Company {name: 'ASML Holding'}) MERGE (c)-[:MENTIONS]->(a);
MATCH (c:Chunk {chunk_id: 'internal_note_c1'}), (nv:Company {name: 'NVIDIA'}) MERGE (c)-[:MENTIONS]->(nv);
MATCH (c:Chunk {chunk_id: 'internal_note_c1'}), (r:RiskFactor {title: 'Advanced foundry / supply concentration'}) MERGE (c)-[:MENTIONS]->(r);
