"""The identifier spine — SEC `company_tickers.json` + `company_tickers_mf.json` as fast lookups.

`Spine.load()` reads the committed fixture subset (offline) and indexes it three ways:
  * `by_ticker`   — exact issuer ticker → company record,
  * `by_norm`     — normalized issuer title → company record (alias hit),
  * `funds`/`fund_by_series`/`fund_by_ticker` — the mutual-fund series spine (L2 seed).

CIKs are normalized to the 10-digit zero-padded form used everywhere else in the graph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize, normalize_ticker

DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "resolution"


def pad_cik(cik: str | int) -> str:
    """SEC canonical 10-digit zero-padded CIK string (e.g. 1045810 → '0001045810')."""
    return str(cik).strip().lstrip("0").zfill(10) if str(cik).strip() else ""


@dataclass(frozen=True)
class CompanyRecord:
    cik: str
    ticker: str
    title: str
    norm: str


@dataclass(frozen=True)
class FundRecord:
    cik: str
    series_id: str
    class_ids: tuple[str, ...] = field(default_factory=tuple)
    tickers: tuple[str, ...] = field(default_factory=tuple)


class Spine:
    """In-memory issuer + fund identifier spine built from the SEC ticker files."""

    def __init__(
        self, companies: list[CompanyRecord], funds: list[FundRecord]
    ) -> None:
        self.companies = companies
        self.funds = funds
        self.by_ticker: dict[str, CompanyRecord] = {}
        self.by_norm: dict[str, CompanyRecord] = {}
        for c in companies:
            if c.ticker:
                self.by_ticker.setdefault(c.ticker, c)
            if c.norm:
                self.by_norm.setdefault(c.norm, c)
        self.fund_by_series: dict[str, FundRecord] = {f.series_id: f for f in funds}
        self.fund_by_ticker: dict[str, FundRecord] = {}
        for f in funds:
            for t in f.tickers:
                self.fund_by_ticker.setdefault(t, f)

    # -- construction ---------------------------------------------------------------------

    @classmethod
    def load(cls, fixture_dir: str | Path = DEFAULT_FIXTURE_DIR) -> Spine:
        d = Path(fixture_dir)
        companies = cls._load_companies(d / "company_tickers.json")
        funds = cls._load_funds(d / "company_tickers_mf.json")
        return cls(companies, funds)

    @staticmethod
    def _load_companies(path: Path) -> list[CompanyRecord]:
        raw = json.loads(path.read_text())
        # SEC format: {"0": {"cik_str", "ticker", "title"}, ...}
        rows = raw.values() if isinstance(raw, dict) else raw
        out: list[CompanyRecord] = []
        for r in rows:
            title = str(r.get("title", "")).strip()
            out.append(
                CompanyRecord(
                    cik=pad_cik(r.get("cik_str", r.get("cik", ""))),
                    ticker=(normalize_ticker(r.get("ticker")) or ""),
                    title=title,
                    norm=normalize(title),
                )
            )
        return out

    @staticmethod
    def _load_funds(path: Path) -> list[FundRecord]:
        raw = json.loads(path.read_text())
        # SEC format: {"fields": [...], "data": [[...], ...]}
        fields = raw["fields"]
        idx = {name: i for i, name in enumerate(fields)}
        agg: dict[str, dict] = {}
        for row in raw["data"]:
            series_id = str(row[idx["seriesId"]])
            entry = agg.setdefault(
                series_id,
                {"cik": pad_cik(row[idx["cik"]]), "classes": [], "tickers": []},
            )
            class_id = str(row[idx["classId"]])
            if class_id and class_id not in entry["classes"]:
                entry["classes"].append(class_id)
            sym = normalize_ticker(str(row[idx["symbol"]]))
            if sym and sym not in entry["tickers"]:
                entry["tickers"].append(sym)
        return [
            FundRecord(
                cik=v["cik"],
                series_id=sid,
                class_ids=tuple(v["classes"]),
                tickers=tuple(v["tickers"]),
            )
            for sid, v in agg.items()
        ]

    # -- lookups --------------------------------------------------------------------------

    def resolve_ticker(self, ticker: str | None) -> CompanyRecord | None:
        t = normalize_ticker(ticker)
        return self.by_ticker.get(t) if t else None

    def resolve_norm(self, norm: str) -> CompanyRecord | None:
        return self.by_norm.get(norm)

    def resolve_series(self, series_id: str | None) -> FundRecord | None:
        return self.fund_by_series.get(series_id) if series_id else None

    def resolve_fund_ticker(self, ticker: str | None) -> FundRecord | None:
        t = normalize_ticker(ticker)
        return self.fund_by_ticker.get(t) if t else None
