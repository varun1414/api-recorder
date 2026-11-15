#!/usr/bin/env python3
"""A self-contained demo: record a baseline, simulate vendor drift, diff them.

Run it with no arguments and no network access::

    python examples/demo.py

It starts a fake billing API on loopback, records a subscription flow against
"v1" of that API, then restarts it as a drifted "v2" and records the same flow
again. Finally it prints the inspect table, the behaviour report and the diff --
which is the whole library in about ten seconds.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from apirecorder import Recorder, analyse, diff_analyses, load_captures  # noqa: E402
from apirecorder.report import (  # noqa: E402
    render_diff,
    render_inspect,
    render_report,
)

HERE = Path(__file__).resolve().parent
RECORDINGS = HERE / "recordings"


# --------------------------------------------------------------------------
# a fake billing API, in two versions
# --------------------------------------------------------------------------

V1 = {
    ("POST", "/v1/subscriptions"): (
        201,
        {"x-api-version": "2026-04-01"},
        {
            "id": "sub_9f21",
            "customer": "cus_42",
            "plan": "pro",
            "status": "active",
            "trial_end": None,
            "current_period_end": 1763251200,
        },
    ),
    ("GET", "/v1/subscriptions/sub_9f21"): (
        200,
        {"x-api-version": "2026-04-01"},
        {
            "id": "sub_9f21",
            "customer": "cus_42",
            "plan": "pro",
            "status": "active",
            "trial_end": None,
            "current_period_end": 1763251200,
        },
    ),
    ("PATCH", "/v1/subscriptions/sub_9f21"): (
        200,
        {"x-api-version": "2026-04-01"},
        {
            "id": "sub_9f21",
            "customer": "cus_42",
            "plan": "enterprise",
            "status": "active",
            "trial_end": None,
            "current_period_end": 1763251200,
        },
    ),
    ("GET", "/v1/subscriptions"): (
        200,
        {"x-api-version": "2026-04-01"},
        {"data": [{"id": "sub_9f21", "plan": "enterprise"}], "has_more": True},
    ),
    ("DELETE", "/v1/subscriptions/sub_9f21"): (204, {"x-api-version": "2026-04-01"}, None),
}

# Six months later. Creation went asynchronous, pagination was renamed, a field
# was added, and one field became nullable.
V2 = {
    ("POST", "/v1/subscriptions"): (
        202,  # was 201
        {"x-api-version": "2026-10-01"},
        {
            "id": "sub_3b02",
            "customer": "cus_42",
            "plan": "pro",
            "status": "pending",
            "trial_end": None,
            # current_period_end is gone -- not known yet on an async create
        },
    ),
    ("GET", "/v1/subscriptions/sub_3b02"): (
        200,
        {"x-api-version": "2026-10-01"},
        {
            "id": "sub_3b02",
            "customer": "cus_42",
            "plan": "pro",
            "status": "active",
            "trial_end": None,
            "current_period_end": 1763251200,
            "cancel_at": None,  # added
        },
    ),
    ("PATCH", "/v1/subscriptions/sub_3b02"): (
        200,
        {"x-api-version": "2026-10-01"},
        {
            "id": "sub_3b02",
            "customer": "cus_42",
            "plan": "enterprise",
            "status": "active",
            "trial_end": None,
            "current_period_end": 1763251200,
            "cancel_at": None,
        },
    ),
    ("GET", "/v1/subscriptions"): (
        200,
        {"x-api-version": "2026-10-01"},
        # has_more -> next_cursor: the silent pagination break
        {"data": [{"id": "sub_3b02", "plan": None}], "next_cursor": "cur_8812"},
    ),
    ("DELETE", "/v1/subscriptions/sub_3b02"): (204, {"x-api-version": "2026-10-01"}, None),
}


class _Server:
    def __init__(self, routes):
        self.routes = routes

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "FakeBilling/1.0"

            def log_message(self, *args):
                pass

            def _handle(inner):
                path = urlsplit(inner.path).path
                length = int(inner.headers.get("Content-Length") or 0)
                if length:
                    inner.rfile.read(length)

                status, headers, body = routes.get(
                    (inner.command, path), (404, {}, {"error": "not found"})
                )
                payload = b"" if body is None else json.dumps(body).encode()
                inner.send_response(status)
                for name, value in headers.items():
                    inner.send_header(name, value)
                if payload:
                    inner.send_header("Content-Type", "application/json")
                inner.send_header("Content-Length", str(len(payload)))
                inner.end_headers()
                if payload:
                    inner.wfile.write(payload)

            do_GET = do_POST = do_PATCH = do_DELETE = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


# --------------------------------------------------------------------------
# the integration code under test -- note it knows nothing about apirecorder
# --------------------------------------------------------------------------


def run_integration(base: str, subscription_id: str) -> None:
    session = requests.Session()
    session.headers["Authorization"] = "Bearer sk_live_do_not_record_me"

    session.post(
        "%s/v1/subscriptions" % base, json={"customer": "cus_42", "plan": "pro"}
    )
    session.get("%s/v1/subscriptions/%s" % (base, subscription_id))
    session.patch(
        "%s/v1/subscriptions/%s" % (base, subscription_id),
        json={"plan": "enterprise"},
    )
    session.get("%s/v1/subscriptions" % base, params={"limit": "2"})
    session.delete("%s/v1/subscriptions/%s" % (base, subscription_id))


def record(routes, subscription_id: str, destination: Path) -> None:
    server = _Server(routes)
    try:
        recorder = Recorder(output=str(destination), redact=["Authorization"])
        with recorder.capture("subscription_lifecycle"):
            run_integration(server.base, subscription_id)
        recorder.save()
    finally:
        server.close()


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78 + "\n")


def main() -> int:
    if RECORDINGS.exists():
        shutil.rmtree(RECORDINGS)

    baseline_dir = RECORDINGS / "baseline"
    current_dir = RECORDINGS / "today"

    record(V1, "sub_9f21", baseline_dir)
    record(V2, "sub_3b02", current_dir)

    baseline = load_captures(baseline_dir)
    current = load_captures(current_dir)

    banner("apirecorder inspect examples/recordings/baseline")
    print(render_inspect(baseline))

    banner("apirecorder report examples/recordings/baseline")
    print(render_report(baseline, title="Billing API behaviour"))

    banner("apirecorder diff examples/recordings/baseline examples/recordings/today")
    result = diff_analyses(
        analyse(baseline),
        analyse(current),
        baseline_label="examples/recordings/baseline",
        current_label="examples/recordings/today",
    )
    print(render_diff(result))
    print("exit code: %d" % result.exit_code())

    secret_found = any(
        "sk_live" in path.read_text(encoding="utf-8")
        for path in RECORDINGS.rglob("*.json")
    )
    print("\nbearer token present anywhere on disk: %s" % secret_found)
    return result.exit_code()


if __name__ == "__main__":
    sys.exit(main())
