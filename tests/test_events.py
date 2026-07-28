"""SSE envelope tests: to_sse() shape + JSON round-trip."""

from __future__ import annotations

import json

from api.contracts.events import EventType, SSEEvent, make_event


def test_make_event_to_sse_shape():
    ev = make_event(EventType.EXTRACTED, "job-1", seq=3, entities=5, relations=2, dropped=1)
    sse = ev.to_sse()
    assert set(sse.keys()) == {"event", "data"}
    assert sse["event"] == "extracted"


def test_to_sse_data_round_trips_json():
    ev = make_event(EventType.WRITTEN, "job-9", seq=7, nodes=10, edges=4, rejected=0)
    sse = ev.to_sse()
    payload = json.loads(sse["data"])
    assert payload["event"] == "written"
    assert payload["job_id"] == "job-9"
    assert payload["seq"] == 7
    assert payload["data"] == {"nodes": 10, "edges": 4, "rejected": 0}


def test_event_model_defaults():
    ev = SSEEvent(event=EventType.JOB_STARTED, job_id="j")
    assert ev.seq == 0
    assert ev.data == {}
    assert ev.to_sse()["event"] == "job.started"
