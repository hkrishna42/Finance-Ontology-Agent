"""M7 Regulatory Reporting Assistant.

Deterministic, cited regulatory artifacts from the graph:
  * thirteen_f  — SEC-shaped 13F information-table draft (XML) + reviewer CSV (golden-tested).
  * coverage    — principal-risks coverage check (gaps + overcoverage), cited both ways.
  * report_pack — Jinja HTML "Exposure & Concentration Report" + provenance appendix +
                  inputs-snapshot JSON + SHA-256 registered in SQLite + DERIVED_FROM edges.
"""

from __future__ import annotations
