# apirecorder

Record what a third-party API **actually does**, then diff it over time.

Wrap your existing integration code in a capture block. You get a redacted JSON
recording of the real traffic, a behaviour report you can commit next to the
code, and a `diff` that fails CI when the vendor changes something your code
relies on.

```
BREAKING  POST /v1/subscriptions
          primary status 201 -> 202
BREAKING  POST /v1/subscriptions 201->202
          `current_period_end` (number) is gone - it was present in all 1 baseline response(s)
BREAKING  GET /v1/subscriptions 200
          `has_more` (boolean) is gone - it was present in all 1 baseline response(s)
ADDITIVE  GET /v1/subscriptions 200
          `next_cursor` (string) added
```

That is a real run. The vendor made creation asynchronous and renamed the
pagination field — so one integration would start throwing `KeyError`, and
another would silently stop after the first page. Both found before deploy, by a
job that needed no assertions written for them.

---

## Why not just write unit tests?

You should — for the handful of fields and status codes your code genuinely
depends on. `assert resp["id"]` is a statement of contract and gives a precise
failure. For a four-endpoint integration, write the four tests and skip this.

The problem is that **a test only checks what you thought to check.**

Nobody writes `assert resp.status_code == 201` on a create they expect to
succeed; they write `raise_for_status()`, which passes happily on a 202. Nobody
asserts that `has_more` exists, because `while resp["has_more"]` just worked —
until it was renamed and the loop started exiting after one page, with every
test still green.

`apirecorder` compares the *whole* observed shape, including the fields your code
doesn't read yet and the ones you never knew were there. It classifies what it
finds, so additive vendor changes don't fail CI and get `--update`'d
reflexively.

It is snapshot testing, specialised for HTTP. The HTTP-specific parts are what
make it worth more than a generic snapshot library here:

- redaction **before** anything is written to disk, not scrubbed afterwards
- path templating, so `/subs/sub_9f21` and `/subs/sub_3b02` are one operation
- breaking vs additive vs info classification
- coverage gaps: the paths your recordings never exercised at all

**Use both.** Assert the ten things you depend on; diff everything else.

---

## Install

```bash
pip install apirecorder
```

There are no required dependencies. `apirecorder` patches whichever HTTP client
you already use, and everything else is standard library. If you want the client
pulled in as well:

```bash
pip install "apirecorder[requests]"
```

```bash
pip install "apirecorder[httpx]"
```

Supports Python 3.8+, `requests` 2.20+ and `httpx` 0.23+ (sync and async).

### From source

```bash
git clone https://github.com/varun1414/api-recorder.git
```

```bash
cd api-recorder && pip install -e ".[dev]"
```

---

## Quickstart

### 1. Record

The only change to your code is the `with` block. Everything inside it is
untouched — responses reach your code as they always did, and exceptions
propagate normally. The recorder only observes.

```python
import requests
from apirecorder import Recorder

rec = Recorder(output="recordings/billing", redact=["Authorization"])

with rec.capture("subscription_lifecycle"):
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {TOKEN}"

    sub = s.post("https://api.billing.com/v1/subscriptions",
                 json={"customer": "cus_42", "plan": "pro"}).json()

    s.get(f"https://api.billing.com/v1/subscriptions/{sub['id']}")
    s.patch(f"https://api.billing.com/v1/subscriptions/{sub['id']}",
            json={"plan": "enterprise"})
    s.get("https://api.billing.com/v1/subscriptions?limit=2")
    s.delete(f"https://api.billing.com/v1/subscriptions/{sub['id']}")

rec.save()
```

This writes `recordings/billing/subscription_lifecycle.json`:

```json
{
  "seq": 1,
  "request": {
    "method": "POST",
    "url": "https://api.billing.com/v1/subscriptions",
    "path": "/v1/subscriptions",
    "query": {},
    "headers": {"content-type": "application/json", "authorization": "[REDACTED]"},
    "body": {"kind": "json", "value": {"customer": "cus_42", "plan": "pro"}}
  },
  "response": {
    "status": 201,
    "headers": {"content-type": "application/json", "x-api-version": "2026-04-01"},
    "body": {
      "kind": "json",
      "value": {
        "id": "sub_9f21", "customer": "cus_42", "plan": "pro",
        "status": "active", "trial_end": null, "current_period_end": 1763251200
      }
    }
  },
  "duration_ms": 312
}
```

The bearer token is `[REDACTED]` because it was never written, not because it
was cleaned up afterwards.

### 2. Look at what you captured

```bash
apirecorder inspect recordings/billing
```

```
subscription_lifecycle - 5 interaction(s), 1.4s total  [recorded 2026-11-15T08:14:55+00:00]

#  method  endpoint                status  ms   note
-  ------  ----------------------  ------  ---  -----
1  POST    /v1/subscriptions       201     312
2  GET     /v1/subscriptions/{id}  200     88
3  PATCH   /v1/subscriptions/{id}  200     201
4  GET     /v1/subscriptions       200     143
5  DELETE  /v1/subscriptions/{id}  204     96   empty
```

### 3. Write down what the API did

```bash
apirecorder report recordings/billing -o docs/billing-behaviour.md
```

The report has two sections that are kept strictly apart.

**Observed** — per operation: status codes seen, every response field with its
type, whether it was ever null, whether it was sometimes missing, latency, the
stable response headers, and the call sequence.

**Not observed** — gaps in the recording:

```
- No 4xx or 5xx response recorded for `POST /v1/subscriptions` - its error handling is unexercised.
- No 429 recorded anywhere - any rate-limit or retry path never ran.
- No 401 or 403 recorded anywhere - auth failure and token refresh paths never ran.
- The last recorded response from `GET /v1/subscriptions` still had `has_more` set, so the final page was never fetched.
- `trial_end` was null in every recorded POST /v1/subscriptions 201 response - its real type is unknown.
- Nothing written by `/v1/subscriptions` was ever read back with a GET, so the persisted shape is unverified.
```

Those are the lines you cannot get by reading your own code. Commit the report
next to the integration, and commit `recordings/billing/` as your baseline.

### 4. Diff, forever after

Run the same flow against the vendor's sandbox on a schedule, into a different
directory, then:

```bash
apirecorder diff recordings/billing recordings/billing-ci
```

`diff` exits **1** when something breaking appears, **0** otherwise — so it drops
straight into CI.

---

## Try it without an API

The repo ships a self-contained demo: it starts a fake billing API on loopback,
records a baseline, restarts it as a drifted version, and prints all three
outputs. No network access needed.

```bash
python examples/demo.py
```

---

## How changes are classified

| Severity | Meaning | Fails CI |
|---|---|---|
| `breaking` | Something your code could already rely on changed: a primary status code, a field that was always present, a type, a field that became nullable or optional | yes |
| `additive` | The API gained a field or an endpoint. Safe for existing code | only with `--strict` |
| `info` | Worth a glance, not a contract change: a new status code seen for the first time, a stable response header whose value moved, a big latency shift, or an operation one side didn't exercise | no |

Two decisions worth knowing about:

**Coverage differences are not breaking.** If the baseline recorded
`GET /subscriptions` and this run didn't, that usually means your script
changed, not the API. Calling it breaking would train people to ignore the
output.

**A status change doesn't hide the body change underneath it.** When a create
goes `201 -> 202`, the old and new bodies share no status group, so the fields
are compared across the pairing and reported as `POST /subs 201->202`. The
dropped field is the part that actually breaks callers.

---

## CLI reference

```bash
apirecorder inspect RECORDINGS
```

```bash
apirecorder report RECORDINGS [-o OUT.md] [--title TITLE]
```

```bash
apirecorder diff BASELINE CURRENT [-o OUT] [--format text|markdown] [--strict] [--no-info]
```

`RECORDINGS` is a capture file or a directory of them. Non-capture JSON in a
directory is skipped.

| Flag | Effect |
|---|---|
| `--strict` | Exit 1 on additive-only changes too |
| `--no-info` | Hide info-level lines from text output |
| `--format markdown` | Render the diff as Markdown, for a PR comment |

Exit codes: `0` nothing breaking, `1` breaking change found, `2` bad usage.

---

## In CI

```yaml
name: api-drift
on:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:

jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install -e ".[requests]"

      - name: Record today's run against the vendor sandbox
        env:
          BILLING_TOKEN: ${{ secrets.BILLING_SANDBOX_TOKEN }}
        run: python scripts/record_billing_flow.py --out recordings/billing-ci

      - name: Diff against the committed baseline
        run: apirecorder diff recordings/billing recordings/billing-ci

      - name: Publish the drift report
        if: failure()
        run: |
          apirecorder diff recordings/billing recordings/billing-ci \
            --format markdown -o drift.md || true

      - uses: actions/upload-artifact@v4
        if: failure()
        with:
          name: drift-report
          path: drift.md
```

When it goes red, read the diff, then either fix your integration or update the
baseline deliberately — never both at once, and never by reflex.

---

## Redaction

Redaction runs while the interaction is still in memory. Nothing unredacted is
ever written.

Always redacted, whether you ask or not:

- **Headers**: `authorization`, `proxy-authorization`, `cookie`, `set-cookie`,
  `x-api-key`, `api-key`, `x-auth-token`, `x-csrf-token`
- **Body and query fields**: `password`, `secret`, `client_secret`,
  `access_token`, `refresh_token`, `id_token`, `api_key`, `private_key`,
  `session_token`

Add your own:

```python
Recorder(redact=["X-Tenant-Secret", "ssn"])
```

Names are matched after lowercasing and stripping `-`/`_`, so one entry covers
`X-Tenant-Secret`, `x_tenant_secret` and `xTenantSecret`. Matching is on the
whole name, so redacting `token` does not blank out `tokenizer_version`.

Redaction reaches nested objects and arrays, form-encoded bodies (so a
`password=` in a login POST is caught), query parameters, and the stored URL.

**Still check your recordings before committing them.** A secret in an
unexpected place — a signed URL in a response body, PII in a payload — is not
something a name-based rule can find for you.

---

## What gets recorded

| Thing | Behaviour |
|---|---|
| JSON bodies | Parsed and stored |
| Form-encoded bodies | Parsed into a mapping, so fields can be redacted |
| Text bodies | Stored as text |
| Binary bodies | Content type and size only |
| Streamed bodies | Not stored — reading them would break your code |
| Bodies over 256 KiB | Content type and size only |
| Redirects | One record per hop |
| Transport failures | Recorded with the exception type, then re-raised |

Only JSON and form bodies take part in analysis and diffing. A recording never
implies it knows more than it saw.

---

## Limitations

Worth knowing before you rely on it.

- **SDK-level capture only.** The hooks patch `requests` and `httpx`. An
  integration using `aiohttp`, `urllib3` directly, or a vendor SDK with its own
  transport is not captured. A proxy mode would cover those and isn't built yet.
- **Patching is global and process-wide.** A capture block records traffic from
  every thread, and two `Recorder`s cannot capture at once (it raises rather
  than silently interleaving). Nested blocks are rejected.
- **Path templating is a heuristic.** A segment is treated as an id if it looks
  like one — a UUID, all digits, long hex, a prefixed id like `sub_9f21`, or a
  long opaque string containing a digit. Ordinary words are left alone, so
  `/users/me` stays `/users/me`. It errs towards under-templating, because
  merging two genuinely different operations is the worse failure.
- **A diff is only as good as the flow you record.** It compares what both runs
  exercised. An endpoint your script never calls cannot drift in the report.
- **No replay or mock server yet.** This records and compares; it does not serve
  recorded responses back.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest
```

The capture tests run against a real loopback HTTP server rather than a mocked
transport — patching a client and asserting against a mock would only test the
mock, not whether the patch points sit where the traffic actually is.

---

## Roadmap

- **0.2** — replay: return recorded responses from the same patch points, for
  offline tests
- **0.3** — OpenAPI generation from the analysis pass
- **0.4** — pytest fixture/test generation; optional proxy capture for clients
  the SDK hooks can't see

---

## License

MIT. See [LICENSE](LICENSE).
