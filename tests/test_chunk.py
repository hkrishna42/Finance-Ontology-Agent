from api.extract.chunk import approx_tokens, chunk_document

DOC = """ITEM 1A. RISK FACTORS

We depend on a limited number of foundries, principally TSMC, for fabrication of our GPUs.
A limited number of hyperscale customers account for a substantial portion of revenue.

ITEM 7. MANAGEMENT DISCUSSION

Revenue grew substantially year over year driven by data center demand.
Gross margin expanded on favorable product mix.
"""


def test_chunks_have_valid_offsets_that_slice_back():
    chunks = chunk_document(DOC, target_tokens=40, hard_max=60)
    assert len(chunks) >= 2
    for ch in chunks:
        assert 0 <= ch.start <= ch.end <= len(DOC)
        # the recorded text is the (stripped) slice of the source at those offsets
        assert ch.text.strip() in DOC


def test_headings_start_new_chunks():
    chunks = chunk_document(DOC, target_tokens=1000, hard_max=1200)
    headings = [c.heading for c in chunks if c.heading]
    assert any("ITEM 1A" in (h or "") for h in headings)
    assert any("ITEM 7" in (h or "") for h in headings)


def test_indices_are_sequential():
    chunks = chunk_document(DOC, target_tokens=40, hard_max=60)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_approx_tokens_monotonic():
    assert approx_tokens("a" * 400) > approx_tokens("a" * 40)
