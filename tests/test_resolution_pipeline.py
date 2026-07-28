"""M2 / resolution — normalization, spine lookups, alias collapse, embedding+LLM, provisional queue.

Fully offline: SEC spine from committed fixtures, GLEIF mocked in-memory, embedder injected, LLM via
FakeProvider (stub) reading the committed fixtures/fake adjudication result.
"""

from __future__ import annotations

import pytest

from api.providers.fake import FakeProvider
from api.resolution import store as queue_store
from api.resolution.gleif import StaticGleifClient
from api.resolution.normalize import normalize, normalize_ticker
from api.resolution.resolver import Resolver
from api.resolution.spine import Spine, pad_cik
from api.stores.sqlite import connect

NVDA_CIK = "0001045810"
NVDA_LEI = "549300N1KM4WQE9DTA96"


# --- normalization -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NVIDIA Corporation", "nvidia"),
        ("NVIDIA Corp", "nvidia"),
        ("NVIDIA", "nvidia"),
        ("Apple Inc.", "apple"),
        ("Advanced Micro Devices, Inc.", "advanced micro devices"),
        ("Eli Lilly & Co", "eli lilly"),
        ("Johnson and Johnson", "johnson johnson"),
        ("ASML Holding NV", "asml"),
        ("Taiwan Semiconductor Manufacturing Co Ltd", "taiwan semiconductor manufacturing"),
    ],
)
def test_normalize_folds_suffixes(raw, expected):
    assert normalize(raw) == expected


def test_normalize_ticker():
    assert normalize_ticker(" nvda ") == "NVDA"
    assert normalize_ticker("BRK.B") == "BRK-B"
    assert normalize_ticker(None) is None


def test_pad_cik():
    assert pad_cik(1045810) == "0001045810"
    assert pad_cik("1045810") == "0001045810"
    assert pad_cik("0001045810") == "0001045810"


# --- spine ---------------------------------------------------------------------------------


def test_spine_loads_companies_and_funds():
    sp = Spine.load()
    assert sp.resolve_ticker("NVDA").cik == NVDA_CIK
    assert sp.resolve_norm("apple").ticker == "AAPL"
    # mutual-fund spine (L2 seed)
    growth = sp.resolve_series("S000090001")
    assert growth is not None and growth.cik == "0009000001"
    assert "DGFAX" in growth.tickers
    assert sp.resolve_fund_ticker("DFGAX").series_id == "S000090002"


# --- ACCEPTANCE: aliases collapse to one CIK/LEI node --------------------------------------


def test_aliases_collapse_to_single_cik_and_lei():
    sp = Spine.load()
    gleif = StaticGleifClient({"NVIDIA CORP": NVDA_LEI})
    resolver = Resolver(spine=sp, gleif=gleif)

    variants = [
        ("NVIDIA", None),
        ("NVIDIA Corp", None),
        ("NVIDIA Corporation", None),
        ("nvidia corp.", None),
        ("NVDA", "NVDA"),  # ticker-driven
    ]
    results = [resolver.resolve(name, ticker=tkr) for name, tkr in variants]

    assert all(r.resolved for r in results)
    assert {r.cik for r in results} == {NVDA_CIK}  # one CIK
    assert {r.lei for r in results} == {NVDA_LEI}  # one LEI
    assert {r.method for r in results} == {"alias", "ticker"}


# --- embedding-similarity → LLM adjudication (uses committed fixtures/fake result) ---------


class _NvidiaEmbedder:
    """Deterministic offline embedder: any text mentioning 'nvidia' shares one axis; else another.

    Only the NVIDIA spine title crosses cosine 0.90 to the query, so exactly one candidate reaches
    adjudication — matching the committed fixtures/fake adjudication call.
    """

    dim = 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] if "nvidia" in t else [0.0, 1.0, 0.0, 0.0] for t in texts]


def test_embedding_then_llm_adjudication_resolves():
    sp = Spine.load()
    gleif = StaticGleifClient({"NVIDIA CORP": NVDA_LEI})
    # FakeProvider reads the committed fixtures/fake adjudication result (match → CIK 0001045810).
    resolver = Resolver(
        spine=sp,
        embedder=_NvidiaEmbedder(),
        provider=FakeProvider(),
        gleif=gleif,
    )
    # A mention that does NOT normalize onto any spine title (so ticker/alias miss → embedding).
    res = resolver.resolve("Nvidia Corp (the chip maker)")
    assert res.resolved
    assert res.method == "embedding_llm"
    assert res.cik == NVDA_CIK
    assert res.lei == NVDA_LEI
    assert res.confidence >= 0.9


def test_llm_no_match_defaults_to_provisional_and_queues():
    sp = Spine.load()
    conn = queue_store.init_resolution_db(connect(":memory:"))
    # FakeProvider with no matching fixture → minimal instance → match:false → provisional.
    resolver = Resolver(spine=sp, embedder=_NvidiaEmbedder(), provider=FakeProvider())
    res = resolver.resolve("Totally Unknown Widgets LLC", conn=conn)
    assert not res.resolved
    assert res.status == "provisional"
    assert res.queue_id is not None

    queued = queue_store.list_queue(conn, status="provisional")
    assert len(queued) == 1
    assert queued[0]["mention"] == "Totally Unknown Widgets LLC"
    conn.close()


def test_resolve_fund_by_series_and_ticker():
    resolver = Resolver(spine=Spine.load())
    by_series = resolver.resolve_fund(series_id="S000090002")
    assert by_series.resolved and by_series.cik == "0009000002"
    by_ticker = resolver.resolve_fund(ticker="DGFAX")
    assert by_ticker.resolved and by_ticker.title == "S000090001"
