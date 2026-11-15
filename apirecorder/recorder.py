"""The public recording API."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from . import _patches
from .model import Capture, Interaction
from .redact import Redactor

_install_lock = threading.Lock()
_installed = False


class Recorder:
    """Records real HTTP traffic made inside :meth:`capture` blocks.

    ::

        rec = Recorder(output="recordings/billing", redact=["Authorization"])
        with rec.capture("subscription_lifecycle"):
            run_my_integration()
        rec.save()

    The capture block is a pass-through. Responses reach the caller untouched
    and exceptions propagate normally -- the recorder only observes.
    """

    def __init__(
        self,
        output: str = "recordings",
        redact: Iterable[str] = (),
        placeholder: str = "[REDACTED]",
    ):
        self.output = Path(output)
        self.redactor = Redactor(redact, placeholder=placeholder)
        self._captures: Dict[str, Capture] = {}
        self._order: List[str] = []
        self._current: Optional[Capture] = None

    # -- recording --------------------------------------------------------

    @contextmanager
    def capture(self, name: str) -> Iterator[Capture]:
        """Record every HTTP call made in this block under ``name``.

        Re-using a name appends to that capture, so a flow can be recorded in
        several blocks and still come out as one ordered sequence.
        """
        global _installed

        if self._current is not None:
            raise RuntimeError(
                "capture() blocks cannot be nested (already recording %r)"
                % self._current.name
            )

        with _install_lock:
            if _installed:
                raise RuntimeError(
                    "another Recorder is already capturing in this process"
                )
            _installed = True

        capture = self._captures.get(name)
        if capture is None:
            capture = Capture(name=name)
            self._captures[name] = capture
            self._order.append(name)

        self._current = capture
        try:
            with _patches.install(self._sink):
                yield capture
        finally:
            self._current = None
            with _install_lock:
                _installed = False

    def _sink(self, interaction: Interaction) -> None:
        """Redact, then store. Nothing unredacted is ever retained."""
        if self._current is None:  # pragma: no cover - defensive
            return

        request = interaction.request
        request.headers = self.redactor.headers(request.headers)
        request.query = self.redactor.query(request.query)
        request.body = self.redactor.body(request.body)
        request.url = _redact_url_query(request.url, self.redactor)

        if interaction.response is not None:
            response = interaction.response
            response.headers = self.redactor.headers(response.headers)
            response.body = self.redactor.body(response.body)

        self._current.add(interaction)

    # -- access -----------------------------------------------------------

    @property
    def captures(self) -> List[Capture]:
        return [self._captures[name] for name in self._order]

    def interactions(self) -> List[Interaction]:
        return [i for capture in self.captures for i in capture.interactions]

    # -- output -----------------------------------------------------------

    def save(self, destination: Optional[str] = None) -> List[Path]:
        """Write one ``<name>.json`` per capture. Returns the paths written."""
        target = Path(destination) if destination is not None else self.output
        target.mkdir(parents=True, exist_ok=True)
        return [
            capture.write(target / ("%s.json" % _safe_filename(capture.name)))
            for capture in self.captures
        ]


def _safe_filename(name: str) -> str:
    keep = []
    for char in name:
        keep.append(char if (char.isalnum() or char in "-_.") else "_")
    return "".join(keep).strip("._") or "capture"


def _redact_url_query(url: str, redactor: Redactor) -> str:
    """Strip secrets out of the stored URL's query string.

    The URL is kept for readability, but an ``?access_token=`` in it would be a
    secret on disk, so it is rewritten from the already-redacted parameters.
    """
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url

    params = parse_qs(parts.query, keep_blank_values=True)
    if not any(redactor.is_secret_field(name) for name in params):
        return url

    cleaned = redactor.query(params)
    query = urlencode(cleaned, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
