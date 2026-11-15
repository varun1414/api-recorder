from __future__ import annotations

import pytest
from factories import capture, interaction

from apirecorder.analyze import analyse, gaps, json_type, template_path, walk


# -- path templating -------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/v1/subscriptions/sub_9f21", "/v1/subscriptions/{id}"),
        ("/v1/subscriptions/cus-4821", "/v1/subscriptions/{id}"),
        ("/users/12345", "/users/{id}"),
        (
            "/users/3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "/users/{id}",
        ),
        ("/blobs/a3f91c0bd2e45871", "/blobs/{id}"),
        ("/orgs/org_12/users/usr_99", "/orgs/{id}/users/{id}"),
        ("/reports/2026/11", "/reports/{id}/{id}"),
        ("", "/"),
    ],
)
def test_identifier_segments_are_templated(path, expected):
    assert template_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/v1/subscriptions",
        "/users/me",
        "/users/current_user",
        "/v1/user_profile",
        "/health",
        "/api/v2/accounts/settings",
    ],
)
def test_ordinary_words_are_left_alone(path):
    # Over-templating is worse than under-templating: it merges operations that
    # are genuinely different.
    assert template_path(path) == path


# -- shape walking ---------------------------------------------------------


def test_json_type_names():
    assert json_type(None) == "null"
    assert json_type(True) == "boolean"
    assert json_type(1) == json_type(1.5) == "number"
    assert json_type("a") == "string"
    assert json_type({}) == "object"
    assert json_type([]) == "array"


def test_walk_yields_containers_and_leaves():
    found = dict(walk({"id": "x", "meta": {"n": 1}, "tags": ["a", "b"]}))
    assert found["id"] == "string"
    assert found["meta"] == "object"
    assert found["meta.n"] == "number"
    assert found["tags"] == "array"
    assert found["tags[]"] == "string"


def test_list_elements_collapse_onto_one_path():
    paths = [p for p, _ in walk({"data": [{"id": 1}, {"id": 2}, {"id": 3}]})]
    assert paths.count("data[].id") == 3  # one per element
    assert len({p for p in paths}) == 3   # but only three distinct paths


# -- aggregation -----------------------------------------------------------


def test_operations_group_by_method_and_template():
    cap = capture(
        "flow",
        interaction("GET", "/v1/subs/sub_1", 200, {"id": "sub_1"}),
        interaction("GET", "/v1/subs/sub_2", 200, {"id": "sub_2"}),
        interaction("POST", "/v1/subs", 201, {"id": "sub_3"}),
    )
    analysis = analyse([cap])

    assert len(analysis.operations) == 2
    get_op = analysis.operations[("GET", "/v1/subs/{id}")]
    assert get_op.count == 2
    assert analysis.interaction_count == 3
    assert analysis.endpoint_count == 2


def test_statuses_are_kept_apart():
    cap = capture(
        "flow",
        interaction("POST", "/users", 201, {"id": 1, "name": "a"}),
        interaction("POST", "/users", 422, {"error": "bad name"}),
    )
    op = analyse([cap]).operations[("POST", "/users")]

    assert sorted(op.statuses) == [201, 422]
    assert "name" in op.statuses[201].fields
    # The 422 body's fields must not leak into the 201 shape.
    assert "error" not in op.statuses[201].fields
    assert op.primary_status == 201


def test_field_presence_and_nullability():
    cap = capture(
        "flow",
        interaction("GET", "/u", 200, {"id": 1, "nick": "a", "bio": None}),
        interaction("GET", "/u", 200, {"id": 2, "bio": "hello"}),
    )
    fields = analyse([cap]).operations[("GET", "/u")].statuses[200].fields

    assert fields["id"].always_present
    assert fields["id"].type_label == "number"

    assert not fields["nick"].always_present
    assert (fields["nick"].present, fields["nick"].total) == (1, 2)

    assert fields["bio"].nullable
    assert fields["bio"].type_label == "string|null"


def test_always_null_field_is_flagged():
    cap = capture(
        "flow",
        interaction("GET", "/u", 200, {"trial_end": None}),
        interaction("GET", "/u", 200, {"trial_end": None}),
    )
    stat = analyse([cap]).operations[("GET", "/u")].statuses[200].fields["trial_end"]
    assert stat.always_null
    assert stat.type_label == "null"


def test_latency_summary():
    cap = capture(
        "flow",
        interaction("GET", "/u", 200, {}, duration_ms=100),
        interaction("GET", "/u", 200, {}, duration_ms=200),
        interaction("GET", "/u", 200, {}, duration_ms=900),
    )
    op = analyse([cap]).operations[("GET", "/u")]
    assert (op.min_ms, op.p50_ms, op.max_ms) == (100, 200, 900)


def test_measured_zero_counts_but_unmeasured_hops_do_not():
    # A loopback call really can take under a millisecond; an untimed redirect
    # hop recovered from a client's history has no duration at all. Folding the
    # second into the first would drag every p50 towards zero.
    cap = capture(
        "flow",
        interaction("GET", "/u", 200, {}, duration_ms=0),
        interaction("GET", "/u", 200, {}, duration_ms=200),
        interaction("GET", "/u", 200, {}, duration_ms=None),
    )
    op = analyse([cap]).operations[("GET", "/u")]
    assert op.durations == [0, 200]
    assert op.p50_ms == 100
    assert op.count == 3


def test_operation_with_no_timings_reports_none():
    cap = capture("flow", interaction("GET", "/u", 200, {}, duration_ms=None))
    op = analyse([cap]).operations[("GET", "/u")]
    assert op.p50_ms is None and op.max_ms is None and op.min_ms is None


def test_stable_headers_exclude_volatile_and_varying_ones():
    cap = capture(
        "flow",
        interaction(
            "GET", "/u", 200, {},
            response_headers={"x-api-version": "1", "x-request-id": "a", "x-node": "n1"},
        ),
        interaction(
            "GET", "/u", 200, {},
            response_headers={"x-api-version": "1", "x-request-id": "b", "x-node": "n2"},
        ),
    )
    stable = analyse([cap]).operations[("GET", "/u")].stable_headers()
    assert stable["x-api-version"] == "1"
    assert "x-request-id" not in stable  # known volatile
    assert "x-node" not in stable        # varied across calls


def test_unstructured_bodies_contribute_no_fields():
    cap = capture("flow", interaction("GET", "/logo.png", 200, None))
    group = analyse([cap]).operations[("GET", "/logo.png")].statuses[200]
    assert group.count == 1
    assert group.bodies_analysed == 0
    assert group.fields == {}


def test_failed_interactions_are_counted():
    cap = capture("flow", interaction("GET", "/u", None, error="ConnectionError"))
    analysis = analyse([cap])
    assert analysis.failed_count == 1
    assert analysis.operations[("GET", "/u")].errors["ConnectionError"] == 1


# -- gaps ------------------------------------------------------------------


def _gap_text(*captures):
    return "\n".join(gaps(list(captures)))


def test_missing_error_statuses_are_reported():
    text = _gap_text(capture("flow", interaction("GET", "/u", 200, {"id": 1})))
    assert "No 429 recorded" in text
    assert "No 401 or 403 recorded" in text
    assert "No 5xx recorded" in text
    assert "No 4xx or 5xx response recorded for `GET /u`" in text


def test_recorded_error_statuses_are_not_reported_as_gaps():
    text = _gap_text(
        capture(
            "flow",
            interaction("GET", "/u", 200, {"id": 1}),
            interaction("GET", "/u", 429, {"error": "slow down"}),
            interaction("GET", "/u", 401, {"error": "nope"}),
            interaction("GET", "/u", 503, {"error": "down"}),
        )
    )
    assert "No 429 recorded" not in text
    assert "No 401 or 403" not in text
    assert "No 5xx recorded" not in text


def test_unfinished_pagination_is_reported():
    text = _gap_text(
        capture(
            "flow",
            interaction("GET", "/items", 200, {"data": [1, 2], "has_more": True}),
        )
    )
    assert "final page was never fetched" in text
    assert "has_more" in text


def test_completed_pagination_is_not_reported():
    text = _gap_text(
        capture(
            "flow",
            interaction("GET", "/items", 200, {"data": [1, 2], "has_more": True}),
            interaction("GET", "/items", 200, {"data": [3], "has_more": False}),
        )
    )
    assert "final page was never fetched" not in text


def test_write_never_read_back_is_reported():
    text = _gap_text(capture("flow", interaction("POST", "/users", 201, {"id": 1})))
    assert "was ever read back with a GET" in text


def test_write_followed_by_read_is_not_reported():
    text = _gap_text(
        capture(
            "flow",
            interaction("POST", "/users", 201, {"id": 1}),
            interaction("GET", "/users/1", 200, {"id": 1}),
        )
    )
    assert "was ever read back with a GET" not in text


def test_always_null_field_appears_in_gaps():
    text = _gap_text(capture("flow", interaction("GET", "/u", 200, {"trial_end": None})))
    assert "`trial_end` was null in every recorded" in text
