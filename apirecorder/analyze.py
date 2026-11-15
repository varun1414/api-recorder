"""Turning recordings into a description of what the API did.

Everything here is a pure function over a list of interactions. Nothing in this
module talks to the network or the filesystem, which is what lets the report and
the diff share one implementation.

Two rules this module holds to:

* Observed and inferred are never mixed. :class:`Analysis` only ever says what
  appeared in the recordings; :func:`gaps` only ever says what did *not* appear.
  Neither makes a claim about how the API behaves in general.
* Response shapes are grouped by status code. A 201 body and a 422 body have
  nothing to do with each other, and merging them would make every field look
  "sometimes missing".
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .model import STRUCTURED_KINDS, Capture, Interaction

#: Response field names that usually mean "there is more to fetch".
PAGINATION_HINTS = (
    "has_more",
    "hasmore",
    "has_next",
    "hasnext",
    "next",
    "next_page",
    "nextpage",
    "next_cursor",
    "nextcursor",
    "next_page_token",
    "nextpagetoken",
    "cursor",
    "after",
    "offset",
    "page_token",
    "continuation_token",
)

#: Headers whose values change on every call; never worth diffing.
VOLATILE_HEADERS = frozenset(
    {
        "date",
        "x-request-id",
        "request-id",
        "x-correlation-id",
        "x-trace-id",
        "traceparent",
        "etag",
        "last-modified",
        "set-cookie",
        "content-length",
        "age",
        "expires",
        "x-runtime",
        "cf-ray",
        "x-amz-request-id",
        "x-amz-id-2",
    }
)

#: Above this many distinct values, a header is treated as volatile.
_MAX_HEADER_VALUES = 4

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_ALL_DIGITS = re.compile(r"^\d+$")
_LONG_HEX = re.compile(r"^[0-9a-f]{16,}$", re.I)
# "sub_9f21", "cus-4821", "org_12" -- a short word, a separator, then something
# containing a digit. The ``(?!v\d+$)`` guard keeps version-like segments such
# as "api_v2" out, since those are route names rather than identifiers.
_PREFIXED_ID = re.compile(
    r"^[A-Za-z]{2,12}[_-](?!v\d+$)(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+$"
)
# Long opaque tokens such as base62 ids; the digit requirement keeps real words out.
_OPAQUE_ID = re.compile(r"^(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{12,}$")


# --------------------------------------------------------------------------
# path templating
# --------------------------------------------------------------------------


def is_identifier_segment(segment: str) -> bool:
    return bool(
        _UUID.match(segment)
        or _ALL_DIGITS.match(segment)
        or _LONG_HEX.match(segment)
        or _PREFIXED_ID.match(segment)
        or _OPAQUE_ID.match(segment)
    )


def template_path(path: str) -> str:
    """Replace identifier-looking segments with ``{id}``.

    So ``/v1/subscriptions/sub_9f21`` and ``/v1/subscriptions/sub_3b02`` both
    become ``/v1/subscriptions/{id}`` and group into one operation instead of
    twenty.

    This is a heuristic on purpose, and it is deliberately conservative: a
    segment has to look like an identifier (a UUID, all digits, long hex, a
    prefixed id, or a long opaque string containing a digit) to be replaced.
    A segment that is an ordinary word is left alone, so ``/users/me`` stays
    ``/users/me`` rather than collapsing into ``/users/{id}``.
    """
    if not path:
        return "/"
    parts = path.split("/")
    return "/".join(
        "{id}" if part and is_identifier_segment(part) else part for part in parts
    )


# --------------------------------------------------------------------------
# response shape
# --------------------------------------------------------------------------


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "unknown"


def walk(value: Any, prefix: str = "") -> Iterator[Tuple[str, str]]:
    """Yield ``(path, json_type)`` for every node, containers included.

    Containers are yielded too, so a field changing from ``array`` to ``object``
    is visible rather than only showing up as its children moving around.
    List elements collapse onto one ``field[]`` path, so a 50-item list
    describes one shape rather than fifty.
    """
    if prefix:
        yield prefix, json_type(value)

    if isinstance(value, dict):
        for key, inner in value.items():
            child = "%s.%s" % (prefix, key) if prefix else str(key)
            for item in walk(inner, child):
                yield item
    elif isinstance(value, (list, tuple)):
        child = "%s[]" % prefix if prefix else "[]"
        for element in value:
            for item in walk(element, child):
                yield item


@dataclass
class FieldStat:
    """What was seen at one field path, across the responses in a status group."""

    path: str
    types: Set[str] = field(default_factory=set)
    #: Responses in which this path existed.
    present: int = 0
    #: Responses in which it existed and was null.
    nulls: int = 0
    #: Responses in the group that had a structured body at all.
    total: int = 0

    @property
    def always_present(self) -> bool:
        return self.total > 0 and self.present == self.total

    @property
    def always_null(self) -> bool:
        return self.present > 0 and self.nulls == self.present

    @property
    def nullable(self) -> bool:
        return self.nulls > 0

    @property
    def type_label(self) -> str:
        """A readable type, with ``null`` folded in as a nullable marker."""
        concrete = sorted(t for t in self.types if t != "null")
        if not concrete:
            return "null"
        label = "|".join(concrete)
        return label + "|null" if "null" in self.types else label


@dataclass
class StatusGroup:
    status: int
    count: int = 0
    #: Responses in this group whose body was structured (JSON or form).
    bodies_analysed: int = 0
    fields: "OrderedDict[str, FieldStat]" = field(default_factory=OrderedDict)

    def record(self, body: Dict[str, Any]) -> None:
        self.count += 1
        if not isinstance(body, dict) or body.get("kind") not in STRUCTURED_KINDS:
            return

        self.bodies_analysed += 1
        seen: Dict[str, Set[str]] = OrderedDict()
        for path, kind in walk(body.get("value")):
            seen.setdefault(path, set()).add(kind)

        for path, kinds in seen.items():
            stat = self.fields.get(path)
            if stat is None:
                stat = FieldStat(path=path)
                self.fields[path] = stat
            stat.present += 1
            stat.types |= kinds
            if "null" in kinds:
                stat.nulls += 1

        # Every field's ``total`` tracks the group, not just the rows it
        # appeared in, so "present in 2 of 5" is answerable.
        for stat in self.fields.values():
            stat.total = self.bodies_analysed


@dataclass
class Operation:
    method: str
    template: str
    count: int = 0
    durations: List[int] = field(default_factory=list)
    statuses: "OrderedDict[int, StatusGroup]" = field(default_factory=OrderedDict)
    #: Response header name -> observed values (``None`` once treated volatile).
    header_values: "OrderedDict[str, Optional[Set[str]]]" = field(
        default_factory=OrderedDict
    )
    #: Transport failures, by exception type name.
    errors: Counter = field(default_factory=Counter)
    #: Example URLs, for the report. Capped.
    examples: List[str] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str]:
        return self.method, self.template

    @property
    def label(self) -> str:
        return "%s %s" % (self.method, self.template)

    @property
    def primary_status(self) -> Optional[int]:
        """The status seen most often; ties break towards the lower code."""
        if not self.statuses:
            return None
        return sorted(
            self.statuses.values(), key=lambda g: (-g.count, g.status)
        )[0].status

    @property
    def p50_ms(self) -> Optional[int]:
        if not self.durations:
            return None
        return int(statistics.median(self.durations))

    @property
    def max_ms(self) -> Optional[int]:
        return max(self.durations) if self.durations else None

    @property
    def min_ms(self) -> Optional[int]:
        return min(self.durations) if self.durations else None

    def stable_headers(self) -> Dict[str, str]:
        """Headers whose value never varied across this operation's responses."""
        out = {}
        for name, values in self.header_values.items():
            if values and len(values) == 1:
                out[name] = next(iter(values))
        return out

    def record(self, interaction: Interaction) -> None:
        self.count += 1
        if len(self.examples) < 3 and interaction.request.url not in self.examples:
            self.examples.append(interaction.request.url)

        if interaction.error:
            self.errors[interaction.error] += 1

        if interaction.response is None:
            return

        # A measured 0ms (loopback, cache hit) is data; an unmeasured hop is not.
        if interaction.duration_ms is not None:
            self.durations.append(interaction.duration_ms)

        status = interaction.response.status
        group = self.statuses.get(status)
        if group is None:
            group = StatusGroup(status=status)
            self.statuses[status] = group
        group.record(interaction.response.body)

        for name, value in interaction.response.headers.items():
            lowered = name.lower()
            if lowered in VOLATILE_HEADERS:
                self.header_values[lowered] = None
                continue
            current = self.header_values.get(lowered, set())
            if current is None:
                continue
            current = set(current)
            current.add(value)
            self.header_values[lowered] = (
                None if len(current) > _MAX_HEADER_VALUES else current
            )


@dataclass
class Analysis:
    """Everything observed across a set of recordings."""

    operations: "OrderedDict[Tuple[str, str], Operation]" = field(
        default_factory=OrderedDict
    )
    capture_names: List[str] = field(default_factory=list)
    interaction_count: int = 0
    failed_count: int = 0
    #: Ordered list of ``(capture_name, method, template, status)`` as recorded.
    sequence: List[Tuple[str, str, str, Optional[int]]] = field(default_factory=list)

    @property
    def endpoint_count(self) -> int:
        return len({template for _, template in self.operations})

    @property
    def total_ms(self) -> int:
        return sum(sum(op.durations) for op in self.operations.values())

    def all_statuses(self) -> Counter:
        counter: Counter = Counter()
        for op in self.operations.values():
            for status, group in op.statuses.items():
                counter[status] += group.count
        return counter

    def sorted_operations(self) -> List[Operation]:
        return sorted(self.operations.values(), key=lambda op: (op.template, op.method))


def analyse(captures: Iterable[Capture]) -> Analysis:
    analysis = Analysis()
    for capture in captures:
        analysis.capture_names.append(capture.name)
        for interaction in capture.interactions:
            analysis.interaction_count += 1
            if interaction.failed:
                analysis.failed_count += 1

            template = template_path(interaction.request.path)
            key = (interaction.request.method, template)
            operation = analysis.operations.get(key)
            if operation is None:
                operation = Operation(method=key[0], template=template)
                analysis.operations[key] = operation
            operation.record(interaction)

            analysis.sequence.append(
                (
                    capture.name,
                    interaction.request.method,
                    template,
                    interaction.response.status if interaction.response else None,
                )
            )
    return analysis


# --------------------------------------------------------------------------
# coverage gaps
# --------------------------------------------------------------------------


def _is_pagination_field(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1].replace("[]", "")
    return leaf.lower().replace("-", "").replace("_", "") in {
        hint.replace("_", "") for hint in PAGINATION_HINTS
    }


def _truthy_pagination(body: Dict[str, Any]) -> List[str]:
    """Pagination fields in this body that say more data exists."""
    if not isinstance(body, dict) or body.get("kind") not in STRUCTURED_KINDS:
        return []

    found = []
    value = body.get("value")
    if not isinstance(value, dict):
        return []
    for key, inner in value.items():
        if not _is_pagination_field(str(key)):
            continue
        if inner in (None, False, "", 0, [], {}):
            continue
        found.append(str(key))
    return found


def gaps(captures: Iterable[Capture], analysis: Optional[Analysis] = None) -> List[str]:
    """What the recordings do *not* cover.

    These are statements about the recording, never about the API. "No 429 was
    recorded" means exactly that -- not that the API has no rate limit.
    """
    captures = list(captures)
    if analysis is None:
        analysis = analyse(captures)

    notes: List[str] = []
    statuses = analysis.all_statuses()

    # -- error paths, per operation
    for op in analysis.sorted_operations():
        if op.statuses and not any(code >= 400 for code in op.statuses):
            notes.append(
                "No 4xx or 5xx response recorded for `%s` - its error handling "
                "is unexercised." % op.label
            )

    # -- error paths, globally
    if not any(code == 429 for code in statuses):
        notes.append(
            "No 429 recorded anywhere - any rate-limit or retry path never ran."
        )
    if not any(code in (401, 403) for code in statuses):
        notes.append(
            "No 401 or 403 recorded anywhere - auth failure and token refresh "
            "paths never ran."
        )
    if not any(500 <= code < 600 for code in statuses):
        notes.append("No 5xx recorded anywhere - server-failure handling never ran.")

    # -- pagination: did the last page of any listing actually get fetched?
    last_success: Dict[Tuple[str, str], Interaction] = {}
    for capture in captures:
        for interaction in capture.interactions:
            if interaction.response is None or interaction.response.status >= 400:
                continue
            key = (interaction.request.method, template_path(interaction.request.path))
            last_success[key] = interaction

    for key, interaction in sorted(last_success.items()):
        indicators = _truthy_pagination(interaction.response.body)
        if indicators:
            notes.append(
                "The last recorded response from `%s %s` still had %s set, so the "
                "final page was never fetched."
                % (key[0], key[1], ", ".join("`%s`" % i for i in indicators))
            )

    # -- writes that were never read back
    written = {
        op.template
        for op in analysis.operations.values()
        if op.method in ("POST", "PUT")
    }
    read_templates = {
        op.template for op in analysis.operations.values() if op.method == "GET"
    }
    for template in sorted(written):
        item_template = template.rstrip("/") + "/{id}"
        if template not in read_templates and item_template not in read_templates:
            notes.append(
                "Nothing written by `%s` was ever read back with a GET, so the "
                "persisted shape is unverified." % template
            )

    # -- fields that never showed a concrete value
    for op in analysis.sorted_operations():
        for group in op.statuses.values():
            for stat in group.fields.values():
                if stat.always_null:
                    notes.append(
                        "`%s` was null in every recorded %s %s response - its real "
                        "type is unknown." % (stat.path, op.label, group.status)
                    )

    # -- transport failures
    if analysis.failed_count:
        notes.append(
            "%d request(s) never got a response (transport failure); those "
            "interactions carry no response shape."
            % analysis.failed_count
        )

    return notes
