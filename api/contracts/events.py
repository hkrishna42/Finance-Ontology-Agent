"""SSE event envelope — the frozen contract between the backend pipeline and the UI.

Every server-sent event carries `{event, job_id, seq, data}`. The UI switches on `event`. Adding
a field is backward-compatible; renaming/removing one is a contract change (orchestrator-owned).

`to_sse()` renders the shape `sse-starlette`'s EventSourceResponse expects: `{"event", "data"}`.
"""

from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    # job lifecycle
    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    ERROR = "error"
    # ingest pipeline
    CLASSIFIED = "classified"
    PARSED = "parsed"
    CHUNKED = "chunked"
    EXTRACTED = "extracted"  # data: {entities: +n, relations: +n, dropped: n}
    RESOLVED = "resolved"
    WRITTEN = "written"  # data: {nodes: n, edges: n, rejected: n}
    # change impact (emitted when a steward write triggers ImpactGraph)
    IMPACT = "impact"  # data: {added, removed, changed, affected_funds, stale_sections, ...}
    # query streaming
    TOKEN = "token"
    ANSWER = "answer"  # data: {answer, citations[], graph_paths[], plan, cypher?, withheld_count}


class SSEEvent(BaseModel):
    event: EventType
    job_id: str
    seq: int = 0
    data: dict = Field(default_factory=dict)

    def to_sse(self) -> dict[str, str]:
        return {"event": self.event.value, "data": self.to_json()}

    def to_json(self) -> str:
        return json.dumps(
            {"event": self.event.value, "job_id": self.job_id, "seq": self.seq, "data": self.data}
        )


def make_event(event: EventType, job_id: str, seq: int = 0, **data) -> SSEEvent:
    return SSEEvent(event=event, job_id=job_id, seq=seq, data=data)
