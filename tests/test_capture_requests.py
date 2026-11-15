"""Capture through the ``requests`` hook, against a real loopback server."""

from __future__ import annotations

import pytest

from apirecorder import Capture, Recorder, load_captures

requests = pytest.importorskip("requests")


def test_records_request_and_response(api, tmp_path):
    api.route(
        "POST",
        "/v1/subscriptions",
        201,
        {"id": "sub_9f21", "plan": "pro", "trial_end": None},
        headers={"X-Api-Version": "2026-04-01"},
    )

    rec = Recorder(output=str(tmp_path))
    with rec.capture("create"):
        response = requests.post(
            api.url("/v1/subscriptions"),
            json={"customer": "cus_42", "plan": "pro"},
            headers={"Authorization": "Bearer supersecret"},
        )

    # The caller still gets the real response, untouched.
    assert response.status_code == 201
    assert response.json()["id"] == "sub_9f21"

    (interaction,) = rec.interactions()
    assert interaction.seq == 1
    assert interaction.request.method == "POST"
    assert interaction.request.path == "/v1/subscriptions"
    assert interaction.request.body == {
        "kind": "json",
        "value": {"customer": "cus_42", "plan": "pro"},
    }
    assert interaction.response.status == 201
    assert interaction.response.body["value"]["id"] == "sub_9f21"
    assert interaction.response.headers["x-api-version"] == "2026-04-01"
    assert interaction.duration_ms >= 0


def test_authorization_is_never_stored(api, tmp_path):
    api.route("GET", "/me", 200, {"id": 1})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("me"):
        requests.get(api.url("/me"), headers={"Authorization": "Bearer supersecret"})

    (interaction,) = rec.interactions()
    assert interaction.request.headers["authorization"] == "[REDACTED]"

    # And it is not anywhere in the file that lands on disk.
    (path,) = rec.save()
    assert "supersecret" not in path.read_text(encoding="utf-8")


def test_secret_query_parameter_is_stripped_from_the_stored_url(api, tmp_path):
    api.route("GET", "/items", 200, {"items": []})

    rec = Recorder(output=str(tmp_path), redact=["access_token"])
    with rec.capture("items"):
        requests.get(api.url("/items"), params={"access_token": "tok_abc", "limit": "2"})

    (interaction,) = rec.interactions()
    assert interaction.request.query["access_token"] == ["[REDACTED]"]
    assert interaction.request.query["limit"] == ["2"]
    assert "tok_abc" not in interaction.request.url

    (path,) = rec.save()
    assert "tok_abc" not in path.read_text(encoding="utf-8")


def test_form_body_is_parsed_so_passwords_can_be_redacted(api, tmp_path):
    api.route("POST", "/login", 200, {"ok": True})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("login"):
        requests.post(api.url("/login"), data={"user": "john", "password": "hunter2"})

    (interaction,) = rec.interactions()
    assert interaction.request.body["kind"] == "form"
    assert interaction.request.body["value"]["user"] == "john"
    assert interaction.request.body["value"]["password"] == "[REDACTED]"


def test_each_redirect_hop_is_recorded(api, tmp_path):
    api.route("GET", "/old", 302, None, headers={"Location": api.url("/new")})
    api.route("GET", "/new", 200, {"ok": True})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("redirect"):
        requests.get(api.url("/old"))

    interactions = rec.interactions()
    assert [i.response.status for i in interactions] == [302, 200]
    assert [i.request.path for i in interactions] == ["/old", "/new"]


def test_error_statuses_are_recorded(api, tmp_path):
    api.route("GET", "/boom", 500, {"error": "kaboom"})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("boom"):
        requests.get(api.url("/boom"))

    (interaction,) = rec.interactions()
    assert interaction.response.status == 500


def test_binary_bodies_are_recorded_as_metadata_only(api, tmp_path):
    api.route(
        "GET", "/logo.png", 200, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        headers={"Content-Type": "image/png"},
    )

    rec = Recorder(output=str(tmp_path))
    with rec.capture("logo"):
        requests.get(api.url("/logo.png"))

    (interaction,) = rec.interactions()
    body = interaction.response.body
    assert body["kind"] == "binary"
    assert body["content_type"] == "image/png"
    assert body["size"] == 72
    assert "value" not in body


def test_streamed_response_body_is_not_consumed(api, tmp_path):
    api.route("GET", "/stream", 200, {"big": "payload"})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("stream"):
        response = requests.get(api.url("/stream"), stream=True)
        # Still readable by the caller, because the recorder did not touch it.
        assert response.json() == {"big": "payload"}

    (interaction,) = rec.interactions()
    assert interaction.response.body["kind"] == "stream"


def test_transport_failure_is_recorded_and_reraised(tmp_path):
    rec = Recorder(output=str(tmp_path))
    with rec.capture("dead"):
        with pytest.raises(requests.exceptions.RequestException):
            # Nothing listens on this port.
            requests.get("http://127.0.0.1:1/nope", timeout=1)

    (interaction,) = rec.interactions()
    assert interaction.failed
    assert interaction.response is None
    assert interaction.error


def test_patches_are_removed_after_the_block(api, tmp_path):
    api.route("GET", "/ping", 200, {"ok": True})
    from requests.adapters import HTTPAdapter

    before = HTTPAdapter.send
    rec = Recorder(output=str(tmp_path))
    with rec.capture("ping"):
        assert HTTPAdapter.send is not before
        requests.get(api.url("/ping"))
    assert HTTPAdapter.send is before

    # Traffic after the block is not recorded.
    requests.get(api.url("/ping"))
    assert len(rec.interactions()) == 1


def test_nested_capture_blocks_are_rejected(tmp_path):
    rec = Recorder(output=str(tmp_path))
    with rec.capture("outer"):
        with pytest.raises(RuntimeError, match="cannot be nested"):
            with rec.capture("inner"):
                pass


def test_two_recorders_cannot_capture_at_once(tmp_path):
    first = Recorder(output=str(tmp_path))
    second = Recorder(output=str(tmp_path))
    with first.capture("a"):
        with pytest.raises(RuntimeError, match="already capturing"):
            with second.capture("b"):
                pass


def test_reusing_a_name_appends_to_one_capture(api, tmp_path):
    api.route("GET", "/ping", 200, {"ok": True})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("flow"):
        requests.get(api.url("/ping"))
    with rec.capture("flow"):
        requests.get(api.url("/ping"))

    assert len(rec.captures) == 1
    assert [i.seq for i in rec.captures[0].interactions] == [1, 2]


def test_save_round_trips_through_disk(api, tmp_path):
    api.route("GET", "/ping", 200, {"ok": True, "n": 1})

    rec = Recorder(output=str(tmp_path / "recordings"))
    with rec.capture("ping flow"):
        requests.get(api.url("/ping"))
    (path,) = rec.save()

    assert path.name == "ping_flow.json"
    reloaded = Capture.read(path)
    assert reloaded.name == "ping flow"
    assert reloaded.interactions[0].response.body["value"] == {"ok": True, "n": 1}

    # And through the directory loader the CLI uses.
    (via_dir,) = load_captures(path.parent)
    assert via_dir.to_dict() == reloaded.to_dict()
