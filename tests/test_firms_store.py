"""Offline unit tests for the SQLite firm registry (``api.firms.store``).

Pure stdlib sqlite3 against an in-memory DB initialised with the shared SCHEMA — no Neo4j and no
network. Covers slugify, upsert/get/list, active-firm switching, delete (including re-activation of
another firm when the active one is removed), ensure_demo_firm idempotency, and sync_from_graph.
"""

from __future__ import annotations

import pytest

from api.firms import store as fs
from api.stores.sqlite import connect, init_db

_FIRM_KEYS = {
    "firm_id", "name", "lei", "cik", "status", "source", "is_active",
    "fund_count", "funds", "identifiers", "created_at", "updated_at",
}


@pytest.fixture()
def conn():
    c = init_db(connect(":memory:"))
    try:
        yield c
    finally:
        c.close()


# --- slugify ------------------------------------------------------------------------------

def test_slugify():
    assert fs.slugify("Demo Investment Management") == "demo_investment_management"
    assert fs.slugify("  Foo & Bar,  Inc. ") == "foo_bar_inc"
    assert fs.slugify("A--B__C") == "a_b_c"
    assert fs.slugify("Already_slug") == "already_slug"


# --- upsert / get -------------------------------------------------------------------------

def test_upsert_returns_slug_and_get_roundtrips(conn):
    fid = fs.upsert_firm(
        conn,
        name="Acme Capital",
        lei="LEI123",
        cik="0001",
        funds=[{"name": "Acme Fund", "series_id": "S1", "cik": "C1", "lei": "L1"}],
        identifiers={"tickers": ["ACM"]},
    )
    assert fid == "acme_capital"
    firm = fs.get_firm(conn, fid)
    assert firm is not None
    assert set(firm) == _FIRM_KEYS
    assert firm["name"] == "Acme Capital"
    assert firm["lei"] == "LEI123" and firm["cik"] == "0001"
    assert firm["status"] == "ready" and firm["source"] == "edgar"
    assert firm["fund_count"] == 1 and firm["funds"][0]["name"] == "Acme Fund"
    assert firm["identifiers"] == {"tickers": ["ACM"]}
    assert firm["is_active"] is True  # first firm bootstraps the active invariant


def test_get_unknown_firm_returns_none(conn):
    assert fs.get_firm(conn, "nobody") is None


def test_upsert_update_preserves_created_at_and_single_row(conn):
    fid = fs.upsert_firm(conn, name="Acme Capital", cik="0001")
    created = fs.get_firm(conn, fid)["created_at"]
    fid2 = fs.upsert_firm(conn, name="Acme Capital", cik="0002", status="onboarding")
    assert fid2 == fid
    firm = fs.get_firm(conn, fid)
    assert firm["cik"] == "0002" and firm["status"] == "onboarding"
    assert firm["created_at"] == created
    assert len(fs.list_firms(conn)) == 1


# --- active switching ---------------------------------------------------------------------

def test_first_firm_is_active_second_is_not(conn):
    a = fs.upsert_firm(conn, name="Alpha")
    b = fs.upsert_firm(conn, name="Bravo")
    assert fs.get_active_firm(conn)["firm_id"] == a
    assert fs.get_firm(conn, b)["is_active"] is False


def test_set_active_firm_switches_and_is_exclusive(conn):
    a = fs.upsert_firm(conn, name="Alpha")
    b = fs.upsert_firm(conn, name="Bravo")
    out = fs.set_active_firm(conn, b)
    assert out["firm_id"] == b and out["is_active"] is True
    assert fs.get_active_firm(conn)["firm_id"] == b
    assert fs.get_firm(conn, a)["is_active"] is False
    actives = [f for f in fs.list_firms(conn) if f["is_active"]]
    assert len(actives) == 1 and actives[0]["firm_id"] == b


def test_set_active_unknown_raises_keyerror(conn):
    fs.upsert_firm(conn, name="Alpha")
    with pytest.raises(KeyError):
        fs.set_active_firm(conn, "ghost")


def test_upsert_activate_flag_takes_over_active(conn):
    a = fs.upsert_firm(conn, name="Alpha")
    b = fs.upsert_firm(conn, name="Bravo", activate=True)
    assert fs.get_active_firm(conn)["firm_id"] == b
    assert fs.get_firm(conn, a)["is_active"] is False


# --- delete -------------------------------------------------------------------------------

def test_delete_nonactive_leaves_active_untouched(conn):
    a = fs.upsert_firm(conn, name="Alpha")
    b = fs.upsert_firm(conn, name="Bravo")
    assert fs.delete_firm(conn, b) is True
    assert fs.get_firm(conn, b) is None
    assert fs.get_active_firm(conn)["firm_id"] == a
    assert fs.delete_firm(conn, "ghost") is False


def test_delete_active_reactivates_most_recent_remaining(conn):
    a = fs.upsert_firm(conn, name="Alpha")
    fs.upsert_firm(conn, name="Bravo")
    c = fs.upsert_firm(conn, name="Charlie")
    assert fs.get_active_firm(conn)["firm_id"] == a
    assert fs.delete_firm(conn, a) is True
    active = fs.get_active_firm(conn)
    assert active is not None and active["firm_id"] == c
    assert fs.get_firm(conn, "bravo")["is_active"] is False


def test_delete_last_firm_leaves_none_active(conn):
    a = fs.upsert_firm(conn, name="Solo")
    assert fs.delete_firm(conn, a) is True
    assert fs.get_active_firm(conn) is None
    assert fs.list_firms(conn) == []


# --- ensure_demo_firm ---------------------------------------------------------------------

def test_ensure_demo_firm_registers_and_is_idempotent(conn):
    fid = fs.ensure_demo_firm(conn)
    assert fid == "demo_investment_management"
    demo = fs.get_firm(conn, fid)
    assert demo["status"] == "demo" and demo["source"] == "demo"
    assert demo["is_active"] is True  # activated because the registry was empty
    assert demo["fund_count"] == 2
    assert {f["name"] for f in demo["funds"]} == {"Demo Growth Fund", "Demo Focused Growth Fund"}
    created = demo["created_at"]
    # second call is a no-op upsert: same id, single row, created_at preserved
    assert fs.ensure_demo_firm(conn) == fid
    assert len(fs.list_firms(conn)) == 1
    assert fs.get_firm(conn, fid)["created_at"] == created


def test_ensure_demo_firm_does_not_steal_active(conn):
    a = fs.upsert_firm(conn, name="Alpha")  # active
    fs.ensure_demo_firm(conn)
    assert fs.get_active_firm(conn)["firm_id"] == a
    assert fs.get_firm(conn, "demo_investment_management")["is_active"] is False


# --- list ordering ------------------------------------------------------------------------

def test_list_firms_ordering_demo_then_active_then_name(conn):
    fs.upsert_firm(conn, name="Zeta Advisors")   # first -> active
    fs.upsert_firm(conn, name="Alpha Advisors")  # inactive
    fs.ensure_demo_firm(conn)                    # demo, inactive (Zeta already active)
    names = [f["name"] for f in fs.list_firms(conn)]
    assert names == ["Demo Investment Management", "Zeta Advisors", "Alpha Advisors"]


# --- sync_from_graph ----------------------------------------------------------------------

def test_sync_from_graph_inserts_absent_and_never_downgrades(conn):
    # Pre-existing onboarded firm (no funds recorded locally yet).
    fs.upsert_firm(conn, name="Onboarded LLC", status="ready", source="edgar")
    graph = [
        {"name": "Demo Investment Management",
         "funds": [{"name": "Demo Growth Fund", "series_id": "S000090001",
                    "cik": "0009000001", "lei": "DEMOGROWTHFUND000001"}]},
        {"name": "Onboarded LLC",
         "funds": [{"name": "Some Fund", "series_id": "S", "cik": "C", "lei": "L"}]},
        {"name": "Fresh Advisors", "funds": []},
        {"funds": []},  # nameless graph rows are skipped
    ]
    fs.sync_from_graph(conn, graph)

    demo = fs.get_firm(conn, "demo_investment_management")
    assert demo is not None and demo["status"] == "demo" and demo["source"] == "demo"
    fresh = fs.get_firm(conn, "fresh_advisors")
    assert fresh is not None and fresh["status"] == "ready" and fresh["source"] == "edgar"
    # Existing row untouched -> funds NOT overwritten from the graph (never downgrade).
    onb = fs.get_firm(conn, "onboarded_llc")
    assert onb["fund_count"] == 0
    assert onb["is_active"] is True  # still the active firm
    assert {f["name"] for f in fs.list_firms(conn)} == {
        "Onboarded LLC", "Demo Investment Management", "Fresh Advisors",
    }
