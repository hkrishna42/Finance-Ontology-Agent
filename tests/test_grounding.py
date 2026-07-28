from api.extract.grounding import filter_extraction, span_grounded
from api.ontology.models import ExtractedEntity, ExtractedRelation, ExtractionResult

CHUNK = (
    "We depend on a limited number of foundries, principally Taiwan Semiconductor "
    "Manufacturing Company (TSMC), for the fabrication of our GPUs, and on advanced "
    "packaging capacity that is currently constrained."
)


def test_exact_substring_grounds():
    assert span_grounded("principally Taiwan Semiconductor Manufacturing Company (TSMC)", CHUNK)


def test_whitespace_and_case_insensitive():
    assert span_grounded("We  depend on a LIMITED number   of foundries", CHUNK)


def test_fuzzy_near_threshold_grounds():
    # minor OCR-ish drift still grounds at 0.9
    assert span_grounded("we depend on a limited number of foundries", CHUNK, threshold=0.9)


def test_hallucinated_span_dropped():
    assert not span_grounded("NVIDIA guarantees unlimited foundry capacity worldwide", CHUNK)


def test_empty_span_not_grounded():
    assert not span_grounded("", CHUNK)
    assert not span_grounded("   ", CHUNK)


def test_filter_extraction_drops_ungrounded():
    result = ExtractionResult(
        entities=[
            ExtractedEntity(label="Company", name="TSMC",
                            span="Taiwan Semiconductor Manufacturing Company (TSMC)", confidence=0.9),
            ExtractedEntity(label="Company", name="Intel",
                            span="Intel is our primary foundry partner", confidence=0.8),  # hallucinated
        ],
        relations=[
            ExtractedRelation(type="SUPPLIES_TO", subject="TSMC", object="NVIDIA",
                              span="a limited number of foundries, principally Taiwan Semiconductor",
                              confidence=0.9),
            ExtractedRelation(type="COMPETES_WITH", subject="NVIDIA", object="Intel",
                              span="NVIDIA competes fiercely with Intel in data center", confidence=0.7),  # ungrounded
        ],
    )
    kept, dropped = filter_extraction(result, CHUNK)
    assert [e.name for e in kept.entities] == ["TSMC"]
    assert [r.type for r in kept.relations] == ["SUPPLIES_TO"]
    assert {d.detail for d in dropped} == {"Intel", "NVIDIA -> Intel"}
    assert all(d.reason == "span_not_grounded" for d in dropped)
