"""M7(a) — 13F info-table draft: OFFLINE golden-file tests (no Neo4j, no network)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from _apps_seed_fixtures import FOCUSED_FUND, GROWTH_FUND, holdings_rows

from api.modules.reg_reports import thirteen_f as tf

NS = "{http://www.sec.gov/edgar/document/thirteenf/informationtable}"


def test_crosswalk_loads():
    cw = tf.load_crosswalk()
    assert cw["NVDA"]["cusip"] == "67066G104"
    assert cw["AMD"]["issuer_name"] == "ADVANCED MICRO DEVICES"
    assert len(cw) == 10


def test_13f_growth_matches_golden():
    res = tf.build_13f(GROWTH_FUND, holdings_rows(GROWTH_FUND))
    assert res["xml"] == tf.golden_path(GROWTH_FUND).read_text()
    assert res["totals"] == {"n_holdings": 9, "total_value_usd": 4480000000}


def test_13f_focused_matches_golden():
    res = tf.build_13f(FOCUSED_FUND, holdings_rows(FOCUSED_FUND))
    assert res["xml"] == tf.golden_path(FOCUSED_FUND).read_text()
    assert res["totals"] == {"n_holdings": 8, "total_value_usd": 2115000000}


def test_13f_xml_is_well_formed_and_sec_shaped():
    res = tf.build_13f(GROWTH_FUND, holdings_rows(GROWTH_FUND))
    root = ET.fromstring(res["xml"])
    tables = root.findall(f"{NS}infoTable")
    assert len(tables) == 9
    first = tables[0]
    assert first.find(f"{NS}nameOfIssuer").text == "NVIDIA CORP"
    assert first.find(f"{NS}cusip").text == "67066G104"
    assert first.find(f"{NS}value").text == "920000000"
    # value ordering is descending
    values = [int(t.find(f"{NS}value").text) for t in tables]
    assert values == sorted(values, reverse=True)


def test_13f_has_draft_watermark():
    res = tf.build_13f(GROWTH_FUND, holdings_rows(GROWTH_FUND))
    assert "DRAFT" in res["xml"] and "NOT FOR FILING" in res["xml"]


def test_reviewer_csv_flags_missing_shares():
    res = tf.build_13f(GROWTH_FUND, holdings_rows(GROWTH_FUND))
    lines = res["reviewer_csv"].strip().splitlines()
    header = lines[0].split(",")
    assert header[0] == "nameOfIssuer" and header[-1] == "flags"
    # every seed holding lacks shares -> MISSING_SHARES on every row; values present (no MISSING_VALUE)
    body = lines[1:]
    assert all("MISSING_SHARES" in row for row in body)
    assert not any("MISSING_VALUE" in row for row in body)
    assert not any("NO_CUSIP" in row for row in body)  # all tickers in the crosswalk


def test_ampersand_is_escaped_in_xml():
    # "LILLY ELI & CO" must be XML-escaped
    res = tf.build_13f(GROWTH_FUND, holdings_rows(GROWTH_FUND))
    assert "LILLY ELI &amp; CO" in res["xml"]
    ET.fromstring(res["xml"])  # still parses
