from __future__ import annotations

from factories import capture, interaction

from apirecorder.analyze import analyse
from apirecorder.diff import ADDITIVE, BREAKING, INFO, diff_analyses


def run_diff(baseline_caps, current_caps):
    return diff_analyses(analyse(baseline_caps), analyse(current_caps))


def details(diff, severity=None):
    changes = diff.changes if severity is None else diff.of(severity)
    return [c.detail for c in changes]


def categories(diff, severity):
    return {c.category for c in diff.of(severity)}


# -- the no-change case ----------------------------------------------------


def test_identical_recordings_produce_no_changes():
    cap = lambda: capture(
        "flow",
        interaction("POST", "/subs", 201, {"id": "sub_1", "plan": "pro"}),
        interaction("GET", "/subs/sub_1", 200, {"id": "sub_1", "plan": "pro"}),
    )
    diff = run_diff([cap()], [cap()])
    assert not diff.has_changes
    assert diff.exit_code() == 0


def test_different_ids_do_not_count_as_a_change():
    # Templating is what makes this work: sub_1 and sub_2 are one operation.
    base = capture("flow", interaction("GET", "/subs/sub_1", 200, {"id": "sub_1"}))
    curr = capture("flow", interaction("GET", "/subs/sub_2", 200, {"id": "sub_2"}))
    assert not run_diff([base], [curr]).has_changes


# -- breaking changes ------------------------------------------------------


def test_removed_field_is_breaking():
    base = capture("f", interaction("POST", "/subs", 201, {"id": 1, "period_end": 99}))
    curr = capture("f", interaction("POST", "/subs", 201, {"id": 1}))
    diff = run_diff([base], [curr])

    assert len(diff.breaking) == 1
    assert "`period_end` (number) is gone" in details(diff, BREAKING)[0]
    assert diff.exit_code() == 1


def test_primary_status_change_is_breaking():
    base = capture("f", interaction("POST", "/subs", 201, {"id": 1}))
    curr = capture("f", interaction("POST", "/subs", 202, {"id": 1}))
    diff = run_diff([base], [curr])

    assert "primary status 201 -> 202" in details(diff, BREAKING)
    assert diff.exit_code() == 1


def test_a_status_change_does_not_hide_field_changes_underneath_it():
    # Regression: field comparison used to run only on status codes present on
    # both sides, so a create going 201 -> 202 masked every field change in the
    # body -- which is the one thing that actually breaks callers.
    base = capture("f", interaction("POST", "/subs", 201, {"id": 1, "period_end": 99}))
    curr = capture("f", interaction("POST", "/subs", 202, {"id": 1}))
    diff = run_diff([base], [curr])

    assert "primary status 201 -> 202" in details(diff, BREAKING)
    assert any("`period_end` (number) is gone" in d for d in details(diff, BREAKING))
    # And the field change is labelled with the status pairing it was seen across.
    gone = [c for c in diff.breaking if c.category == "field"][0]
    assert gone.status == "201->202"
    assert gone.scope == "POST /subs 201->202"


def test_cross_status_comparison_does_not_double_report():
    # 200 exists on both sides, so the primary groups must not be compared again.
    base = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1}),
        interaction("GET", "/u", 200, {"id": 2}),
        interaction("GET", "/u", 404, {"error": "nope"}),
    )
    curr = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1}),
        interaction("GET", "/u", 404, {"error": "nope"}),
    )
    diff = run_diff([base], [curr])
    assert not diff.breaking


def test_type_change_is_breaking():
    base = capture("f", interaction("GET", "/u", 200, {"id": 1}))
    curr = capture("f", interaction("GET", "/u", 200, {"id": "1"}))
    diff = run_diff([base], [curr])

    assert "`id` changed type: number -> string" in details(diff, BREAKING)


def test_field_becoming_nullable_is_breaking():
    base = capture("f", interaction("GET", "/u", 200, {"name": "a"}))
    curr = capture("f", interaction("GET", "/u", 200, {"name": None}))
    diff = run_diff([base], [curr])

    assert "nullable" in categories(diff, BREAKING)


def test_field_becoming_optional_is_breaking():
    base = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1, "email": "a@b.c"}),
        interaction("GET", "/u", 200, {"id": 2, "email": "d@e.f"}),
    )
    curr = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1, "email": "a@b.c"}),
        interaction("GET", "/u", 200, {"id": 2}),
    )
    diff = run_diff([base], [curr])

    assert "`email` is no longer always present (1 of 2 responses)" in details(
        diff, BREAKING
    )


def test_nested_and_list_fields_are_compared():
    base = capture("f", interaction("GET", "/u", 200, {"data": [{"id": 1, "tier": "pro"}]}))
    curr = capture("f", interaction("GET", "/u", 200, {"data": [{"id": 1}]}))
    diff = run_diff([base], [curr])

    assert any("`data[].tier`" in d for d in details(diff, BREAKING))


def test_pagination_rename_is_caught_as_both_breaking_and_additive():
    # The real-world case: a silent pagination contract change.
    base = capture("f", interaction("GET", "/subs", 200, {"data": [], "has_more": True}))
    curr = capture("f", interaction("GET", "/subs", 200, {"data": [], "next_cursor": "c"}))
    diff = run_diff([base], [curr])

    assert any("`has_more` (boolean) is gone" in d for d in details(diff, BREAKING))
    assert any("`next_cursor` (string) added" in d for d in details(diff, ADDITIVE))
    assert diff.exit_code() == 1


# -- additive changes ------------------------------------------------------


def test_added_field_is_additive_and_does_not_fail_by_default():
    base = capture("f", interaction("GET", "/u", 200, {"id": 1}))
    curr = capture("f", interaction("GET", "/u", 200, {"id": 1, "cancel_at": None}))
    diff = run_diff([base], [curr])

    assert not diff.breaking
    assert "`cancel_at` (null) added" in details(diff, ADDITIVE)
    assert diff.exit_code() == 0
    assert diff.exit_code(strict=True) == 1


# -- info changes ----------------------------------------------------------


def test_stable_header_value_change_is_info():
    base = capture(
        "f", interaction("GET", "/u", 200, {}, response_headers={"x-api-version": "2026-04-01"})
    )
    curr = capture(
        "f", interaction("GET", "/u", 200, {}, response_headers={"x-api-version": "2026-10-01"})
    )
    diff = run_diff([base], [curr])

    assert not diff.breaking
    assert any("x-api-version" in d for d in details(diff, INFO))


def test_new_header_is_info():
    base = capture("f", interaction("GET", "/u", 200, {}, response_headers={}))
    curr = capture("f", interaction("GET", "/u", 200, {}, response_headers={"retry-after": "30"}))
    diff = run_diff([base], [curr])

    assert any("`retry-after` appeared" in d for d in details(diff, INFO))


def test_new_status_code_is_info_not_breaking():
    base = capture("f", interaction("GET", "/u", 200, {"id": 1}))
    curr = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1}),
        interaction("GET", "/u", 429, {"error": "slow down"}),
    )
    diff = run_diff([base], [curr])

    assert not diff.breaking
    assert "status 429 recorded for the first time" in details(diff, INFO)


def test_coverage_difference_is_info_not_breaking():
    # The script changed, not the API. Calling this breaking would train people
    # to ignore the output.
    base = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1}),
        interaction("GET", "/groups", 200, {"data": []}),
    )
    curr = capture("f", interaction("GET", "/u", 200, {"id": 1}))
    diff = run_diff([base], [curr])

    assert not diff.breaking
    assert "in the baseline but not exercised in this run" in details(diff, INFO)
    assert diff.exit_code() == 0


def test_latency_regression_is_info():
    base = capture("f", interaction("GET", "/u", 200, {}, duration_ms=100))
    curr = capture("f", interaction("GET", "/u", 200, {}, duration_ms=1200))
    diff = run_diff([base], [curr])

    assert any("p50 moved 100ms -> 1200ms" in d for d in details(diff, INFO))


def test_small_latency_change_is_ignored():
    base = capture("f", interaction("GET", "/u", 200, {}, duration_ms=100))
    curr = capture("f", interaction("GET", "/u", 200, {}, duration_ms=150))
    assert not run_diff([base], [curr]).has_changes


def test_optional_field_disappearing_is_only_info():
    base = capture(
        "f",
        interaction("GET", "/u", 200, {"id": 1, "note": "x"}),
        interaction("GET", "/u", 200, {"id": 2}),
    )
    curr = capture("f", interaction("GET", "/u", 200, {"id": 1}))
    diff = run_diff([base], [curr])

    assert not diff.breaking
    assert any("`note` not seen in this run" in d for d in details(diff, INFO))


# -- comparison is skipped where it would be guesswork ---------------------


def test_unstructured_bodies_are_not_compared():
    base = capture("f", interaction("GET", "/logo.png", 200, None))
    curr = capture("f", interaction("GET", "/logo.png", 200, None))
    assert not run_diff([base], [curr]).has_changes


def test_sorting_puts_breaking_first():
    base = capture("f", interaction("GET", "/u", 200, {"id": 1, "gone": 2}))
    curr = capture("f", interaction("GET", "/u", 200, {"id": 1, "added": 3}))
    diff = run_diff([base], [curr])

    severities = [c.severity for c in diff.sorted_changes()]
    assert severities[0] == BREAKING
    assert severities.index(BREAKING) < severities.index(ADDITIVE)
