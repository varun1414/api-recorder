"""The on-disk recording format.

A capture is a named, ordered sequence of interactions. One interaction is one
real HTTP exchange: a request, the response it got, and how long it took.

Bodies are stored as tagged dicts rather than raw strings so that a consumer can
always tell what it is looking at::

    {"kind": "json",  "value": {...}}       parsed JSON
    {"kind": "form",  "value": {...}}       parsed application/x-www-form-urlencoded
    {"kind": "text",  "value": "..."}       decodable text
    {"kind": "empty"}                       no body
    {"kind": "binary", "content_type": ..., "size": n}      not stored
    {"kind": "stream", "content_type": ...}                 not stored
    {"kind": "too_large", "content_type": ..., "size": n}    not stored

Only ``json`` and ``form`` bodies take part in schema analysis and diffing; the
others carry no field information, which is deliberate -- a recording never
implies it knows more than it saw.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

FORMAT_VERSION = 1

#: Body kinds whose ``value`` is structured data we can analyse.
STRUCTURED_KINDS = ("json", "form")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Request:
    method: str
    url: str
    path: str
    query: Dict[str, List[str]] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    body: Dict[str, Any] = field(default_factory=lambda: {"kind": "empty"})


@dataclass
class Response:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: Dict[str, Any] = field(default_factory=lambda: {"kind": "empty"})


@dataclass
class Interaction:
    seq: int
    request: Request
    #: ``None`` when the call raised before any response arrived.
    response: Optional[Response] = None
    #: ``None`` when the exchange was not timed on its own -- a redirect hop
    #: recovered from an httpx history, for instance. A measured 0 is a real
    #: sub-millisecond call and is kept distinct from "not measured".
    duration_ms: Optional[int] = None
    #: Exception type name, when the call failed in transport.
    error: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.response is None


@dataclass
class Capture:
    name: str
    recorded_at: str = field(default_factory=utc_now_iso)
    interactions: List[Interaction] = field(default_factory=list)
    format_version: int = FORMAT_VERSION

    def add(self, interaction: Interaction) -> None:
        interaction.seq = len(self.interactions) + 1
        self.interactions.append(interaction)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=False)
            fh.write("\n")
        return path

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Capture":
        interactions = []
        for item in raw.get("interactions", []):
            response = item.get("response")
            interactions.append(
                Interaction(
                    seq=item.get("seq", 0),
                    request=Request(**item["request"]),
                    response=Response(**response) if response else None,
                    duration_ms=item.get("duration_ms"),
                    error=item.get("error"),
                )
            )
        return cls(
            name=raw.get("name", "unnamed"),
            recorded_at=raw.get("recorded_at", ""),
            interactions=interactions,
            format_version=raw.get("format_version", FORMAT_VERSION),
        )

    @classmethod
    def read(cls, path: Path) -> "Capture":
        with Path(path).open(encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def load_captures(source: Path) -> List[Capture]:
    """Load one capture file, or every ``*.json`` capture in a directory.

    Files that are not captures in this format are skipped rather than raising,
    so pointing the CLI at a directory of mixed JSON does something sensible.
    """
    source = Path(source)
    if source.is_file():
        return [Capture.read(source)]
    if not source.is_dir():
        raise FileNotFoundError("no such file or directory: %s" % source)

    captures = []
    for path in sorted(source.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and "interactions" in raw:
            captures.append(Capture.from_dict(raw))
    return captures
