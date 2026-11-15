"""Redaction.

This runs while an interaction is still in memory, before anything is written.
There is no window in which a token exists in a file on disk, which is the whole
point -- scrubbing after the fact is not the same guarantee.

Names are matched after normalisation (lowercased, ``-`` and ``_`` stripped), so
one entry covers ``Authorization``, ``authorization``, ``X-API-Key``, ``api_key``
and ``apiKey``. Matching is on the *whole* name, not a substring, so asking to
redact ``token`` does not also blank out ``tokenizer_version``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

PLACEHOLDER = "[REDACTED]"

#: Headers that are never written, whatever the caller asks for.
DEFAULT_HEADERS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "api-key",
    "apikey",
)

#: Body and query parameter names that are never written.
DEFAULT_FIELDS = (
    "password",
    "passwd",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
    "session_token",
)


def normalise(name: str) -> str:
    return name.lower().replace("-", "").replace("_", "")


class Redactor:
    """Blanks sensitive values in headers, query parameters and bodies."""

    def __init__(self, extra: Iterable[str] = (), placeholder: str = PLACEHOLDER):
        self.placeholder = placeholder
        extra_norm = {normalise(name) for name in extra}
        # An extra name applies everywhere -- the caller does not have to know
        # whether the secret shows up as a header, a query param or a body key.
        self._headers = {normalise(h) for h in DEFAULT_HEADERS} | extra_norm
        self._fields = {normalise(f) for f in DEFAULT_FIELDS} | extra_norm

    # -- predicates -------------------------------------------------------

    def is_secret_header(self, name: str) -> bool:
        return normalise(name) in self._headers

    def is_secret_field(self, name: str) -> bool:
        return normalise(name) in self._fields

    # -- appliers ---------------------------------------------------------

    def headers(self, raw: Mapping[str, Any]) -> Dict[str, str]:
        """Lowercase header names and blank the sensitive ones.

        Names are lowercased because HTTP header names are case-insensitive and
        clients disagree about casing; without this, two recordings of the same
        traffic could diff against each other purely on capitalisation.
        """
        out: Dict[str, str] = {}
        for name, value in raw.items():
            key = str(name).lower()
            out[key] = self.placeholder if self.is_secret_header(name) else str(value)
        return out

    def query(self, raw: Mapping[str, List[str]]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for name, values in raw.items():
            if self.is_secret_field(name):
                out[name] = [self.placeholder for _ in values]
            else:
                out[name] = [str(v) for v in values]
        return out

    def body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Redact a tagged body dict in place-safe fashion (returns a copy)."""
        if not isinstance(body, dict) or "value" not in body:
            return body
        return dict(body, value=self.value(body["value"]))

    def value(self, value: Any) -> Any:
        """Walk arbitrary JSON, blanking values under sensitive keys."""
        if isinstance(value, dict):
            return {
                key: (
                    self.placeholder
                    if self.is_secret_field(str(key))
                    else self.value(inner)
                )
                for key, inner in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value
