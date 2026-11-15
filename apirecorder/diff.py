"""Comparing a baseline recording against a fresh one.

This is the feature the library exists for. A test only checks what you thought
to assert; this compares the whole observed shape, including the fields your
code does not read yet and the ones you never knew were there.

Every change is classified, because an unclassified diff fails CI on every
harmless vendor addition and then gets ignored within a month:

``breaking``
    Something your code could already be relying on changed or vanished: a
    primary status code, a field that was always present, a type, or a field
    that became nullable.
``additive``
    The API gained something. Safe for existing code; never fails CI unless
    ``--strict`` is passed.
``info``
    Worth a human glance but not a contract change: a new status code seen for
    the first time, a stable response header whose value moved, a large latency
    shift, or an operation one side simply did not exercise.

Coverage differences are deliberately *not* breaking. If the baseline recorded
``GET /subscriptions`` and this run did not, that usually means the script
changed, not the API -- calling it breaking would train people to ignore the
output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .analyze import Analysis, Operation, StatusGroup

BREAKING = "breaking"
ADDITIVE = "additive"
INFO = "info"

_SEVERITY_ORDER = {BREAKING: 0, ADDITIVE: 1, INFO: 2}

#: Latency has to at least double *and* move this many ms to be worth a line.
_LATENCY_MIN_DELTA_MS = 200


@dataclass(frozen=True)
class Change:
    severity: str
    category: str
    operation: str
    detail: str
    #: Status the change was observed under, where it applies to a body.
    #: Either a code (``"201"``) or a pairing when the code itself moved
    #: (``"201->202"``).
    status: Optional[str] = None

    @property
    def scope(self) -> str:
        if not self.status:
            return self.operation
        return "%s %s" % (self.operation, self.status)

    def __str__(self) -> str:
        return "%-8s %-9s %s: %s" % (
            self.severity.upper(),
            self.category,
            self.scope,
            self.detail,
        )


@dataclass
class Diff:
    changes: List[Change] = field(default_factory=list)
    baseline_label: str = "baseline"
    current_label: str = "current"

    def add(self, *args, **kwargs) -> None:
        self.changes.append(Change(*args, **kwargs))

    def of(self, severity: str) -> List[Change]:
        return [c for c in self.changes if c.severity == severity]

    @property
    def breaking(self) -> List[Change]:
        return self.of(BREAKING)

    @property
    def additive(self) -> List[Change]:
        return self.of(ADDITIVE)

    @property
    def info(self) -> List[Change]:
        return self.of(INFO)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def sorted_changes(self) -> List[Change]:
        return sorted(
            self.changes,
            key=lambda c: (_SEVERITY_ORDER[c.severity], c.operation, c.category, c.detail),
        )

    def exit_code(self, strict: bool = False) -> int:
        if self.breaking:
            return 1
        if strict and self.additive:
            return 1
        return 0


# --------------------------------------------------------------------------


def _compare_fields(
    diff: Diff, label: str, status: str, base: StatusGroup, curr: StatusGroup
) -> None:
    """Compare the response shape of one status group."""
    if not base.bodies_analysed or not curr.bodies_analysed:
        # One side had no structured body (binary, streamed, empty). There is
        # nothing to compare, and guessing would be worse than silence.
        return

    for path, base_stat in base.fields.items():
        curr_stat = curr.fields.get(path)

        if curr_stat is None:
            if base_stat.always_present:
                diff.add(
                    BREAKING,
                    "field",
                    label,
                    "`%s` (%s) is gone - it was present in all %d baseline "
                    "response(s)"
                    % (path, base_stat.type_label, base_stat.total),
                    status,
                )
            else:
                diff.add(
                    INFO,
                    "field",
                    label,
                    "`%s` not seen in this run - it was optional in the baseline "
                    "(%d of %d)" % (path, base_stat.present, base_stat.total),
                    status,
                )
            continue

        base_concrete = {t for t in base_stat.types if t != "null"}
        curr_concrete = {t for t in curr_stat.types if t != "null"}

        if base_concrete and curr_concrete and base_concrete != curr_concrete:
            diff.add(
                BREAKING,
                "type",
                label,
                "`%s` changed type: %s -> %s"
                % (path, base_stat.type_label, curr_stat.type_label),
                status,
            )
        elif not base_stat.nullable and curr_stat.nullable:
            diff.add(
                BREAKING,
                "nullable",
                label,
                "`%s` is now sometimes null (%d of %d) and never was in the "
                "baseline" % (path, curr_stat.nulls, curr_stat.total),
                status,
            )
        elif base_stat.always_null and curr_concrete:
            diff.add(
                INFO,
                "type",
                label,
                "`%s` was always null in the baseline and is now %s"
                % (path, curr_stat.type_label),
                status,
            )

        if base_stat.always_present and not curr_stat.always_present:
            diff.add(
                BREAKING,
                "optional",
                label,
                "`%s` is no longer always present (%d of %d responses)"
                % (path, curr_stat.present, curr_stat.total),
                status,
            )

    for path, curr_stat in curr.fields.items():
        if path in base.fields:
            continue
        diff.add(
            ADDITIVE,
            "field",
            label,
            "`%s` (%s) added" % (path, curr_stat.type_label),
            status,
        )


def _compare_headers(diff: Diff, label: str, base: Operation, curr: Operation) -> None:
    base_stable = base.stable_headers()
    curr_stable = curr.stable_headers()

    for name, base_value in sorted(base_stable.items()):
        curr_value = curr_stable.get(name)
        if curr_value is not None and curr_value != base_value:
            diff.add(
                INFO,
                "header",
                label,
                "response header `%s`: %r -> %r" % (name, base_value, curr_value),
            )

    base_names = {n for n, v in base.header_values.items() if v is not None}
    curr_names = {n for n, v in curr.header_values.items() if v is not None}

    for name in sorted(curr_names - base_names):
        diff.add(
            INFO, "header", label, "response header `%s` appeared" % name
        )
    for name in sorted(base_names - curr_names):
        diff.add(
            INFO, "header", label, "response header `%s` no longer sent" % name
        )


def _compare_latency(diff: Diff, label: str, base: Operation, curr: Operation) -> None:
    base_p50, curr_p50 = base.p50_ms, curr.p50_ms
    if not base_p50 or not curr_p50:
        return
    delta = curr_p50 - base_p50
    if abs(delta) < _LATENCY_MIN_DELTA_MS:
        return
    ratio = curr_p50 / float(base_p50)
    if ratio >= 2 or ratio <= 0.5:
        diff.add(
            INFO,
            "latency",
            label,
            "p50 moved %dms -> %dms (%.1fx)" % (base_p50, curr_p50, ratio),
        )


def _compare_operation(diff: Diff, base: Operation, curr: Operation) -> None:
    label = base.label

    base_primary, curr_primary = base.primary_status, curr.primary_status
    if base_primary is not None and curr_primary is not None:
        if base_primary != curr_primary:
            diff.add(
                BREAKING,
                "status",
                label,
                "primary status %d -> %d" % (base_primary, curr_primary),
            )

    base_codes = set(base.statuses)
    curr_codes = set(curr.statuses)

    for code in sorted(curr_codes - base_codes):
        diff.add(
            INFO,
            "status",
            label,
            "status %d recorded for the first time" % code,
        )
    for code in sorted(base_codes - curr_codes):
        diff.add(
            INFO,
            "status",
            label,
            "status %d was in the baseline but not recorded in this run" % code,
        )

    for code in sorted(base_codes & curr_codes):
        _compare_fields(
            diff, label, str(code), base.statuses[code], curr.statuses[code]
        )

    # When the status code itself moved, the old and new bodies share no status
    # group, so the loop above compares nothing and every field change hides
    # behind the status line. Compare the two primary groups directly instead --
    # a create going 201 -> 202 and dropping a field is one event, not two, and
    # the dropped field is the part that breaks callers.
    if (
        base_primary is not None
        and curr_primary is not None
        and base_primary != curr_primary
        and curr_primary not in base_codes
    ):
        _compare_fields(
            diff,
            label,
            "%d->%d" % (base_primary, curr_primary),
            base.statuses[base_primary],
            curr.statuses[curr_primary],
        )

    _compare_headers(diff, label, base, curr)
    _compare_latency(diff, label, base, curr)


def diff_analyses(
    baseline: Analysis,
    current: Analysis,
    baseline_label: str = "baseline",
    current_label: str = "current",
) -> Diff:
    result = Diff(baseline_label=baseline_label, current_label=current_label)

    base_ops: Dict[Tuple[str, str], Operation] = dict(baseline.operations)
    curr_ops: Dict[Tuple[str, str], Operation] = dict(current.operations)

    for key in sorted(set(base_ops) & set(curr_ops)):
        _compare_operation(result, base_ops[key], curr_ops[key])

    for key in sorted(set(base_ops) - set(curr_ops)):
        result.add(
            INFO,
            "coverage",
            base_ops[key].label,
            "in the baseline but not exercised in this run",
        )
    for key in sorted(set(curr_ops) - set(base_ops)):
        result.add(
            INFO,
            "coverage",
            curr_ops[key].label,
            "exercised in this run but absent from the baseline",
        )

    return result
