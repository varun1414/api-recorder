"""Builders for recordings, so analysis and diff can be tested without sockets."""

from __future__ import annotations

from typing import Any, Dict, Optional

from apirecorder.model import Capture, Interaction, Request, Response


def body(value: Any = None, kind: str = "json") -> Dict[str, Any]:
    if value is None:
        return {"kind": "empty"}
    return {"kind": kind, "value": value}


def interaction(
    method: str,
    path: str,
    status: Optional[int] = 200,
    response_body: Any = None,
    response_headers: Optional[Dict[str, str]] = None,
    request_body: Any = None,
    duration_ms: int = 10,
    error: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
) -> Interaction:
    request = Request(
        method=method.upper(),
        url="https://api.example.test" + path,
        path=path,
        query={k: [str(v)] for k, v in (query or {}).items()},
        headers={"content-type": "application/json"},
        body=body(request_body),
    )
    response = (
        None
        if status is None
        else Response(
            status=status,
            headers=dict(response_headers or {"content-type": "application/json"}),
            body=body(response_body),
        )
    )
    return Interaction(
        seq=0,
        request=request,
        response=response,
        duration_ms=duration_ms,
        error=error,
    )


def capture(name: str, *interactions: Interaction) -> Capture:
    cap = Capture(name=name, recorded_at="2026-11-15T00:00:00+00:00")
    for item in interactions:
        cap.add(item)
    return cap
