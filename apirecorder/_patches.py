"""Capture hooks for ``requests`` and ``httpx``.

Both hooks are installed by monkeypatching and removed again on exit, so the
caller's integration code needs no changes at all.

Why these particular patch points:

* ``requests.adapters.HTTPAdapter.send`` rather than ``Session.send``, because
  the adapter sees one real exchange per call. Patching ``Session.send`` would
  collapse a redirect chain into a single record and lose the hops.
* ``httpx.Client.send`` / ``AsyncClient.send``, because httpx has no equivalent
  public per-exchange seam. Redirects are recovered afterwards by unrolling
  ``response.history``, which gives the same per-hop result.

Bodies are only stored when they can be read without consuming them. A streamed
request or response is recorded as ``{"kind": "stream"}`` -- reading it to get a
nicer recording would break the caller's code, which is never worth it.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .model import Interaction, Request, Response

#: Bodies larger than this are recorded as metadata only.
MAX_BODY_BYTES = 256 * 1024

_BINARY_HINTS = (
    "image/",
    "video/",
    "audio/",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
)

Sink = Callable[[Interaction], None]


# --------------------------------------------------------------------------
# body + url encoding
# --------------------------------------------------------------------------


def split_url(url: str) -> Tuple[str, Dict[str, List[str]]]:
    parts = urlsplit(url)
    return parts.path or "/", parse_qs(parts.query, keep_blank_values=True)


def _is_binary_content_type(content_type: str) -> bool:
    lowered = (content_type or "").lower()
    return any(hint in lowered for hint in _BINARY_HINTS)


def _looks_like_json(raw: bytes) -> bool:
    head = raw.lstrip()[:1]
    return head in (b"{", b"[")


def encode_body(raw: Any, content_type: Optional[str] = None) -> Dict[str, Any]:
    """Turn a raw request/response body into a tagged body dict.

    Form-encoded bodies are parsed into a mapping on purpose: it is the only way
    a ``password=...`` field in a login POST can be reached by redaction.
    """
    content_type = content_type or ""

    if raw is None or raw == b"" or raw == "":
        return {"kind": "empty"}

    # File-like objects and generators (multipart uploads, streamed sends).
    if hasattr(raw, "read") or hasattr(raw, "__next__"):
        return {"kind": "stream", "content_type": content_type or None}

    if isinstance(raw, str):
        data = raw.encode("utf-8", "replace")
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        return {"kind": "stream", "content_type": content_type or None}

    if len(data) > MAX_BODY_BYTES:
        return {
            "kind": "too_large",
            "content_type": content_type or None,
            "size": len(data),
        }

    lowered = content_type.lower()

    if "x-www-form-urlencoded" in lowered:
        try:
            parsed = parse_qs(data.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            pass
        else:
            # Collapse single-element lists; keep real repeats as lists.
            value = {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}
            return {"kind": "form", "value": value}

    if "json" in lowered or _looks_like_json(data):
        try:
            return {"kind": "json", "value": json.loads(data.decode("utf-8"))}
        except (UnicodeDecodeError, ValueError):
            pass

    if _is_binary_content_type(content_type):
        return {
            "kind": "binary",
            "content_type": content_type or None,
            "size": len(data),
        }

    try:
        return {"kind": "text", "value": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "kind": "binary",
            "content_type": content_type or None,
            "size": len(data),
        }


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------


def _build_requests_interaction(prepared, response, elapsed_ms, streamed):
    path, query = split_url(prepared.url or "")
    req_ct = prepared.headers.get("Content-Type", "")
    request = Request(
        method=(prepared.method or "").upper(),
        url=prepared.url or "",
        path=path,
        query=query,
        headers=dict(prepared.headers or {}),
        body=(
            {"kind": "stream", "content_type": req_ct or None}
            if streamed and prepared.body is not None
            else encode_body(prepared.body, req_ct)
        ),
    )

    if response is None:
        return Interaction(seq=0, request=request, response=None, duration_ms=elapsed_ms)

    resp_ct = response.headers.get("Content-Type", "")
    if streamed:
        body: Dict[str, Any] = {"kind": "stream", "content_type": resp_ct or None}
    else:
        body = encode_body(response.content, resp_ct)

    return Interaction(
        seq=0,
        request=request,
        response=Response(
            status=response.status_code,
            headers=dict(response.headers or {}),
            body=body,
        ),
        duration_ms=elapsed_ms,
    )


def _patch_requests(sink: Sink) -> Optional[Callable[[], None]]:
    try:
        from requests.adapters import HTTPAdapter
    except ImportError:
        return None

    original = HTTPAdapter.send

    def send(self, request, stream=False, *args, **kwargs):
        started = time.perf_counter()
        try:
            response = original(self, request, stream, *args, **kwargs)
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            interaction = _build_requests_interaction(request, None, elapsed, stream)
            interaction.error = type(exc).__name__
            sink(interaction)
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        sink(_build_requests_interaction(request, response, elapsed, stream))
        return response

    HTTPAdapter.send = send

    def restore():
        HTTPAdapter.send = original

    return restore


# --------------------------------------------------------------------------
# httpx
# --------------------------------------------------------------------------


def _build_httpx_interaction(response, elapsed_ms, streamed):
    """Build an interaction from one httpx response and its request."""
    req = response.request
    path, query = split_url(str(req.url))
    req_ct = req.headers.get("content-type", "")

    try:
        raw_request_body: Any = req.content
    except Exception:
        # Streaming request body -- not readable without consuming it.
        raw_request_body = None
        req_body: Dict[str, Any] = {"kind": "stream", "content_type": req_ct or None}
    else:
        req_body = encode_body(raw_request_body, req_ct)

    request = Request(
        method=req.method.upper(),
        url=str(req.url),
        path=path,
        query=query,
        headers=dict(req.headers),
        body=req_body,
    )

    resp_ct = response.headers.get("content-type", "")
    if streamed:
        body: Dict[str, Any] = {"kind": "stream", "content_type": resp_ct or None}
    else:
        try:
            body = encode_body(response.content, resp_ct)
        except Exception:
            body = {"kind": "stream", "content_type": resp_ct or None}

    return Interaction(
        seq=0,
        request=request,
        response=Response(
            status=response.status_code,
            headers=dict(response.headers),
            body=body,
        ),
        duration_ms=elapsed_ms,
    )


def _emit_httpx_chain(sink: Sink, response, elapsed_ms, streamed) -> None:
    """Record each hop of a redirect chain, oldest first.

    httpx resolves redirects inside ``send``, so the intermediate responses are
    only reachable through ``history``. The measured elapsed time covers the
    whole chain, so it is attributed to the final hop and the earlier hops get
    no duration rather than a made-up one.
    """
    for previous in response.history:
        sink(_build_httpx_interaction(previous, None, streamed=False))
    sink(_build_httpx_interaction(response, elapsed_ms, streamed))


def _patch_httpx(sink: Sink) -> Optional[Callable[[], None]]:
    try:
        import httpx
    except ImportError:
        return None

    original_sync = httpx.Client.send
    original_async = httpx.AsyncClient.send

    def send(self, request, **kwargs):
        streamed = bool(kwargs.get("stream", False))
        started = time.perf_counter()
        try:
            response = original_sync(self, request, **kwargs)
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            path, query = split_url(str(request.url))
            interaction = Interaction(
                seq=0,
                request=Request(
                    method=request.method.upper(),
                    url=str(request.url),
                    path=path,
                    query=query,
                    headers=dict(request.headers),
                ),
                response=None,
                duration_ms=elapsed,
                error=type(exc).__name__,
            )
            sink(interaction)
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        _emit_httpx_chain(sink, response, elapsed, streamed)
        return response

    async def asend(self, request, **kwargs):
        streamed = bool(kwargs.get("stream", False))
        started = time.perf_counter()
        try:
            response = await original_async(self, request, **kwargs)
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            path, query = split_url(str(request.url))
            sink(
                Interaction(
                    seq=0,
                    request=Request(
                        method=request.method.upper(),
                        url=str(request.url),
                        path=path,
                        query=query,
                        headers=dict(request.headers),
                    ),
                    response=None,
                    duration_ms=elapsed,
                    error=type(exc).__name__,
                )
            )
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        _emit_httpx_chain(sink, response, elapsed, streamed)
        return response

    httpx.Client.send = send
    httpx.AsyncClient.send = asend

    def restore():
        httpx.Client.send = original_sync
        httpx.AsyncClient.send = original_async

    return restore


# --------------------------------------------------------------------------
# installation
# --------------------------------------------------------------------------


def available_clients() -> List[str]:
    found = []
    for name in ("requests", "httpx"):
        try:
            __import__(name)
        except ImportError:
            continue
        found.append(name)
    return found


@contextmanager
def install(sink: Sink):
    """Patch every installed client for the duration of the block.

    Patching is global, so a capture block records traffic from every thread in
    the process, not just the calling one.
    """
    restores = [
        restore
        for restore in (_patch_requests(sink), _patch_httpx(sink))
        if restore is not None
    ]
    if not restores:
        raise RuntimeError(
            "neither 'requests' nor 'httpx' is installed, so there is nothing "
            "to record. Install one of them, or the 'apirecorder[all]' extra."
        )
    try:
        yield
    finally:
        for restore in reversed(restores):
            restore()
