"""Capture through the ``httpx`` hook, sync and async."""

from __future__ import annotations

import asyncio

import pytest

from apirecorder import Recorder

httpx = pytest.importorskip("httpx")


def test_sync_client_is_recorded(api, tmp_path):
    api.route("POST", "/v1/users", 201, {"id": "usr_1", "active": True})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("create"):
        with httpx.Client() as client:
            response = client.post(
                api.url("/v1/users"),
                json={"username": "john"},
                headers={"Authorization": "Bearer supersecret"},
            )

    assert response.status_code == 201
    (interaction,) = rec.interactions()
    assert interaction.request.method == "POST"
    assert interaction.request.body["value"] == {"username": "john"}
    assert interaction.request.headers["authorization"] == "[REDACTED]"
    assert interaction.response.body["value"]["id"] == "usr_1"


def test_top_level_request_helper_is_recorded(api, tmp_path):
    api.route("GET", "/ping", 200, {"ok": True})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("ping"):
        httpx.get(api.url("/ping"))

    (interaction,) = rec.interactions()
    assert interaction.response.status == 200


def test_redirect_history_is_unrolled_into_hops(api, tmp_path):
    api.route("GET", "/old", 302, None, headers={"Location": api.url("/new")})
    api.route("GET", "/new", 200, {"ok": True})

    rec = Recorder(output=str(tmp_path))
    with rec.capture("redirect"):
        with httpx.Client(follow_redirects=True) as client:
            client.get(api.url("/old"))

    interactions = rec.interactions()
    assert [i.response.status for i in interactions] == [302, 200]
    assert [i.request.path for i in interactions] == ["/old", "/new"]


def test_async_client_is_recorded(api, tmp_path):
    api.route("GET", "/v1/users", 200, {"data": [], "has_more": False})

    rec = Recorder(output=str(tmp_path))

    async def run():
        async with httpx.AsyncClient() as client:
            return await client.get(api.url("/v1/users"))

    with rec.capture("list"):
        response = asyncio.get_event_loop().run_until_complete(run())

    assert response.status_code == 200
    (interaction,) = rec.interactions()
    assert interaction.request.method == "GET"
    assert interaction.response.body["value"] == {"data": [], "has_more": False}


def test_transport_failure_is_recorded_and_reraised(tmp_path):
    rec = Recorder(output=str(tmp_path))
    with rec.capture("dead"):
        with pytest.raises(httpx.HTTPError):
            httpx.get("http://127.0.0.1:1/nope", timeout=1)

    (interaction,) = rec.interactions()
    assert interaction.failed
    assert interaction.error


def test_patches_are_removed_after_the_block(api, tmp_path):
    api.route("GET", "/ping", 200, {"ok": True})
    before_sync = httpx.Client.send
    before_async = httpx.AsyncClient.send

    rec = Recorder(output=str(tmp_path))
    with rec.capture("ping"):
        httpx.get(api.url("/ping"))

    assert httpx.Client.send is before_sync
    assert httpx.AsyncClient.send is before_async
