"""M7(c) — report packs: OFFLINE unit tests (deterministic snapshot, stable SHA, registry)."""

from __future__ import annotations

from _apps_seed_fixtures import GROWTH_FUND, seed_runner

from api.modules.reg_reports import report_pack as rp
from api.stores.sqlite import connect, init_db


def test_snapshot_is_deterministic_and_timestamp_free():
    snap1 = rp.build_snapshot(seed_runner(), GROWTH_FUND)
    snap2 = rp.build_snapshot(seed_runner(), GROWTH_FUND)
    assert snap1 == snap2
    # no wall-clock fields inside the hashed snapshot
    assert "generated_at" not in rp.canonical_json(snap1)
    assert snap1["figures"]["hhi"] == 412.5
    assert snap1["figures"]["coverage_gap"]["gap_categories"] == ["geopolitical", "supply_chain"]


def test_sha256_stable_across_two_runs():
    sha1 = rp.snapshot_sha256(rp.build_snapshot(seed_runner(), GROWTH_FUND))
    sha2 = rp.snapshot_sha256(rp.build_snapshot(seed_runner(), GROWTH_FUND))
    assert sha1 == sha2
    assert len(sha1) == 64


def test_report_id_stable():
    snap = rp.build_snapshot(seed_runner(), GROWTH_FUND)
    rid = rp.report_id_for(snap)
    assert rid.startswith("exposure_concentration:")
    assert snap["period"] == "2026-Q2"  # from as_of 2026-05-31


def test_render_html_contains_key_figures_and_provenance():
    res = rp.build_report(seed_runner(), GROWTH_FUND, generated_at="2026-07-27T00:00:00+00:00")
    html = res["html"]
    assert "Exposure &amp; Concentration Report" in html
    assert "412.5" in html  # HHI
    assert "supply_chain" in html and "geopolitical" in html  # coverage gap
    assert "Appendix A" in html and "Appendix B" in html  # provenance appendix
    assert res["sha256"] in html
    # generated_at is display-only: it does not change the hash
    res2 = rp.build_report(seed_runner(), GROWTH_FUND, generated_at="2099-01-01T00:00:00+00:00")
    assert res2["sha256"] == res["sha256"]


def test_registry_insert_and_idempotent():
    conn = init_db(connect(":memory:"))
    res = rp.build_report(seed_runner(), GROWTH_FUND, conn=conn)
    assert res["registered"] is True
    rows = list(conn.execute("SELECT report_id, sha256 FROM report_registry"))
    assert len(rows) == 1
    assert rows[0]["sha256"] == res["sha256"]
    # second build with same graph => same sha, still one row (upsert)
    rp.build_report(seed_runner(), GROWTH_FUND, conn=conn)
    assert len(list(conn.execute("SELECT * FROM report_registry"))) == 1
    conn.close()


def test_html_lists_uncovered_holdings():
    res = rp.build_report(seed_runner(), GROWTH_FUND, generated_at="2026-07-27T00:00:00+00:00")
    # coverage honesty: Microsoft is uncovered and should be listed
    assert "Microsoft" in res["html"]
