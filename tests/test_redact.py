"""Redaction is tested hardest, because it is the part that must not fail."""

from __future__ import annotations

from apirecorder.redact import PLACEHOLDER, Redactor, normalise


def test_normalise_folds_case_and_separators():
    assert normalise("API_KEY") == normalise("api-key") == normalise("apiKey") == "apikey"
    assert normalise("accessToken") == normalise("access_token") == "accesstoken"
    # The x- prefix is part of the name, so both spellings are listed by default.
    assert normalise("X-API-Key") == "xapikey"


def test_both_spellings_of_the_api_key_header_are_covered():
    r = Redactor()
    assert r.is_secret_header("X-API-Key")
    assert r.is_secret_header("api_key")


def test_default_headers_are_redacted_without_being_asked():
    r = Redactor()
    out = r.headers({"Authorization": "Bearer abc", "Cookie": "s=1", "Accept": "*/*"})
    assert out["authorization"] == PLACEHOLDER
    assert out["cookie"] == PLACEHOLDER
    assert out["accept"] == "*/*"


def test_header_names_are_lowercased():
    # Two clients capitalising differently must not diff against each other.
    r = Redactor()
    assert r.headers({"Content-Type": "application/json"}) == {
        "content-type": "application/json"
    }


def test_extra_name_applies_to_headers_query_and_body():
    r = Redactor(["X-Tenant-Secret"])
    assert r.headers({"x-tenant-secret": "s"})["x-tenant-secret"] == PLACEHOLDER
    assert r.query({"x_tenant_secret": ["s"]}) == {"x_tenant_secret": [PLACEHOLDER]}
    assert r.value({"xTenantSecret": "s"}) == {"xTenantSecret": PLACEHOLDER}


def test_default_body_fields_are_redacted():
    r = Redactor()
    out = r.value({"username": "john", "password": "hunter2", "access_token": "t"})
    assert out == {
        "username": "john",
        "password": PLACEHOLDER,
        "access_token": PLACEHOLDER,
    }


def test_redaction_reaches_nested_dicts_and_lists():
    r = Redactor()
    payload = {
        "users": [
            {"name": "a", "password": "p1"},
            {"name": "b", "credentials": {"client_secret": "cs"}},
        ]
    }
    out = r.value(payload)
    assert out["users"][0]["password"] == PLACEHOLDER
    assert out["users"][0]["name"] == "a"
    assert out["users"][1]["credentials"]["client_secret"] == PLACEHOLDER


def test_matching_is_whole_name_not_substring():
    # Asking for "token" must not blank out unrelated fields that contain it.
    r = Redactor(["token"])
    out = r.value({"token": "secret", "tokenizer_version": "3", "token_count": 7})
    assert out["token"] == PLACEHOLDER
    assert out["tokenizer_version"] == "3"
    assert out["token_count"] == 7


def test_body_wrapper_preserves_kind_and_copies():
    r = Redactor()
    body = {"kind": "json", "value": {"password": "p"}}
    out = r.body(body)
    assert out["kind"] == "json"
    assert out["value"]["password"] == PLACEHOLDER
    # The original is untouched.
    assert body["value"]["password"] == "p"


def test_body_wrapper_passes_through_unstructured_bodies():
    r = Redactor()
    for body in ({"kind": "empty"}, {"kind": "binary", "size": 10}, None):
        assert r.body(body) == body


def test_scalars_and_none_survive_the_walk():
    r = Redactor()
    assert r.value(None) is None
    assert r.value(5) == 5
    assert r.value([1, "a", None]) == [1, "a", None]


def test_custom_placeholder():
    r = Redactor(placeholder="***")
    assert r.headers({"authorization": "x"})["authorization"] == "***"
