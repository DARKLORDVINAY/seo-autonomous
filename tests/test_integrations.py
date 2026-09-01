import json

import httpx
import pytest

from backend.app.contracts import ConcurrencyConflict, ProviderUnavailable
from backend.app.integrations.ai_search import AISearchClient
from backend.app.integrations.common import AmbiguousWriteError, MalformedResponse, ProviderError, request
from backend.app.integrations.fixtures import FixtureCMS, fixture_observations
from backend.app.integrations.google_analytics import GA4Client
from backend.app.integrations.google_search_console import GSCClient
from backend.app.integrations.serp import DataForSEOClient
from backend.app.integrations.wordpress import WordPressClient


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def wp_page(**changes):
    page = {"id": 123, "link": "https://example.com/windows/", "title": {"raw": "Windows"},
            "content": {"raw": "<p>Original</p>"}, "status": "publish", "slug": "windows",
            "modified_gmt": "2026-08-01T10:00:00", "meta": {"seo_description": "Original description"}}
    return page | changes


def test_gsc_paginates_final_and_keeps_coverage_uncertainty():
    payloads = []
    def handler(req):
        payload = json.loads(req.content)
        payloads.append(payload)
        offset = payload["startRow"]
        rows = ([{"keys": ["2026-08-01", f"https://example.com/{offset}/", "query"],
                  "clicks": 3, "impressions": 100, "position": 4}] if offset < 2 else [])
        return httpx.Response(200, json={"rows": rows})
    batch = GSCClient("sc-domain:example.com", client=client(handler), token_provider=lambda: "fixture").fetch(
        "2026-08-01", "2026-08-02", page_size=1)
    assert len(batch.rows) == 2 and batch.complete is False
    assert all(p["dataState"] == "final" for p in payloads)
    assert [p["startRow"] for p in payloads] == [0, 1, 2]
    assert batch.metadata["missing_dates"] == ["2026-08-02"]
    assert "anonymised_queries_omitted_do_not_sum_as_page_totals" in batch.quality_flags


@pytest.mark.parametrize("response", [{"rows": "bad"}, {"rows": [{"keys": []}]}, {"rows": [{
    "keys": ["2026-08-01", "https://example.com/", "query"], "clicks": 1, "impressions": 3, "position": -1}]}])
def test_gsc_malformed_is_error_never_zero(response):
    provider = GSCClient("sc-domain:example.com", client=client(lambda req: httpx.Response(200, json=response)),
                         token_provider=lambda: "fixture")
    with pytest.raises(MalformedResponse):
        provider.fetch("2026-08-01", "2026-08-01")


def test_gsc_repeated_pagination_stops():
    row = {"keys": ["2026-08-01", "https://example.com/", "query"], "clicks": 1, "impressions": 3, "position": 1}
    provider = GSCClient("sc-domain:example.com", client=client(lambda req: httpx.Response(200, json={"rows": [row]})),
                         token_provider=lambda: "fixture")
    with pytest.raises(MalformedResponse):
        provider.fetch("2026-08-01", "2026-08-01", page_size=1)


def ga4_report(rows=None, **changes):
    return {"dimensionHeaders": [{"name": d} for d in GA4Client.DIMENSIONS],
            "metricHeaders": [{"name": m} for m in GA4Client.METRICS], "rowCount": 1,
            "rows": rows if rows is not None else [{"dimensionValues": [{"value": "20260801"},
                {"value": "/windows/"}, {"value": "Organic Search"}],
                "metricValues": [{"value": "100"}, {"value": "7.5"}]}]} | changes


def test_ga4_key_events_not_qualified_conversions_and_threshold_flags():
    payloads = []
    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(200, json=ga4_report(metadata={"subjectToThresholding": True}))
    batch = GA4Client("123", client=client(handler), token_provider=lambda: "fixture").fetch("2026-08-01", "2026-08-02")
    row = batch.rows[0]
    assert row.sessions == 100 and row.key_events == 7.5
    assert row.qualified_conversions is None and row.conversion_value is None
    assert "subjectToThresholding" in row.quality_flags and "missing_dates" in batch.quality_flags
    assert payloads[0]["dimensionFilter"]["filter"]["stringFilter"]["value"] == "Organic Search"


def test_ga4_stalled_pagination_is_not_success():
    provider = GA4Client("123", client=client(lambda req: httpx.Response(200, json=ga4_report(rows=[], rowCount=7))),
                         token_provider=lambda: "fixture")
    with pytest.raises(MalformedResponse):
        provider.fetch("2026-08-01", "2026-08-01")


def test_rate_limit_read_bounded_retries():
    calls, delays = [], []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"retry-after": "0"})
    with pytest.raises(ProviderError):
        request(client(handler), "GET", "https://example.com/", sleep=delays.append)
    assert len(calls) == 3 and delays == [0, 0]


def test_wordpress_read_requires_raw_authenticated_content():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=wp_page())
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(handler))
    page = provider.get_page("pages:123")
    assert calls[0].url.params["context"] == "edit" and "authorization" in calls[0].headers
    assert page.external_id == "pages:123" and page.content == "<p>Original</p>"
    with pytest.raises(ValueError):
        provider.get_page("123")


def test_wordpress_inventory_preserves_post_type_ids():
    def handler(req):
        assert req.url.params["context"] == "edit"
        return httpx.Response(200, json=[wp_page()], headers={"X-WP-TotalPages": "1"})
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(handler))
    assert {page.external_id for page in provider.list_pages()} == {"pages:123", "posts:123"}


def test_wordpress_default_blocks_non_atomic_production_writes():
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(lambda req: pytest.fail("No network")))
    with pytest.raises(ProviderUnavailable, match="atomic"):
        provider.update_page("pages:123", {"title": "Changed"}, expected_fingerprint="a")


def test_wordpress_conflict_never_posts():
    calls = []
    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=wp_page())
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(handler), allow_optimistic_writes=True)
    with pytest.raises(ConcurrencyConflict):
        provider.update_page("pages:123", {"title": "Changed"}, expected_fingerprint="stale")
    assert calls == ["GET"]


def test_wordpress_meta_requires_explicit_exposed_key():
    calls = []
    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=wp_page())
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(handler), allow_optimistic_writes=True)
    before = provider.get_page("pages:123")
    with pytest.raises(ProviderUnavailable, match="registered"):
        provider.update_page("pages:123", {"meta_description": "Changed"}, expected_fingerprint=before.fingerprint)
    assert "POST" not in calls


def test_wordpress_transport_failure_does_not_blind_retry_write():
    calls = []
    def handler(req):
        calls.append(req.method)
        if req.method == "POST":
            raise httpx.ReadTimeout("response lost", request=req)
        return httpx.Response(200, json=wp_page())
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(handler), allow_optimistic_writes=True)
    before = provider.get_page("pages:123")
    with pytest.raises(AmbiguousWriteError):
        provider.update_page("pages:123", {"title": "Changed"}, expected_fingerprint=before.fingerprint)
    assert calls.count("POST") == 1


def test_wordpress_update_and_draft_never_send_publish_slug_or_schema():
    calls = []
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=wp_page())
        payload = json.loads(req.content)
        calls.append(payload)
        return httpx.Response(200, json=wp_page(title={"raw": payload.get("title", "Windows")},
                             content={"raw": payload.get("content", "<p>Original</p>")},
                             status=payload.get("status", "publish"),
                             meta=payload.get("meta", {"seo_description": "Original description"})))
    provider = WordPressClient("https://example.com", "fixture", "fixture", client=client(handler),
                               allow_optimistic_writes=True, meta_description_key="seo_description")
    before = provider.get_page("pages:123")
    after = provider.update_page("pages:123", {"meta_description": "New description"}, expected_fingerprint=before.fingerprint)
    assert after.meta_description == "New description" and calls[-1] == {"meta": {"seo_description": "New description"}}
    assert provider.create_draft("Draft", "<p>Draft</p>").status == "draft"
    for changes in ({"status": "publish"}, {"slug": "new"}, {"metadata": {"schema": {}}}):
        with pytest.raises(ValueError):
            provider.update_page("pages:123", changes, expected_fingerprint=after.fingerprint)


def test_fixture_cas_copy_isolation_and_rollback():
    provider = FixtureCMS()
    before = provider.get_page("pages:2")
    external = provider.get_page("pages:2")
    external.metadata["override"] = "malicious"
    assert "override" not in provider.get_page("pages:2").metadata
    after = provider.update_page("pages:2", {"title": "Improved"}, expected_fingerprint=before.fingerprint)
    restored = provider.update_page("pages:2", {"title": before.title}, expected_fingerprint=after.fingerprint)
    assert restored.fingerprint == before.fingerprint
    with pytest.raises(ConcurrencyConflict):
        provider.update_page("pages:2", {"title": "Stale"}, expected_fingerprint=after.fingerprint)
    reconstructed = FixtureCMS(provider.list_pages())
    assert reconstructed.get_page("pages:2").fingerprint == restored.fingerprint


def test_fixture_data_never_claims_live_or_real_qualified_leads():
    observations = fixture_observations()
    assert all(batch.is_fixture for batch in observations.values())
    assert all(row.qualified_conversions is None for row in observations["ga4"].rows)


def test_serp_disabled_and_task_failure_not_zero():
    with pytest.raises(ProviderUnavailable):
        DataForSEOClient().search("windows", 2840)
    provider = DataForSEOClient("fixture", "fixture", enabled=True, client=client(lambda req: httpx.Response(200,
        json={"status_code": 20000, "tasks": [{"status_code": 50000, "result": None}]})))
    with pytest.raises(ProviderError):
        provider.search("windows", 2840)


def test_ai_mode_real_endpoint_and_missing_visibility_unknown():
    assert AISearchClient().status()["visibility"] is None
    with pytest.raises(ProviderUnavailable):
        AISearchClient().search("windows", 2840)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"status_code": 20000, "cost": 0.1,
            "tasks": [{"status_code": 20000, "id": "fixture", "result": [{"items": []}]}]})
    serp = DataForSEOClient("fixture", "fixture", enabled=True, client=client(handler))
    batch = AISearchClient(serp).search("windows", 2840)
    assert calls[0].url.path == "/v3/serp/google/ai_mode/live/advanced"
    assert batch.complete is False and batch.source == "dataforseo:ai_mode"


def test_generic_post_defaults_to_no_retry():
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("ambiguous", request=req)
    with pytest.raises(AmbiguousWriteError):
        request(client(handler), "POST", "https://example.com/", sleep=lambda delay: None)
    assert len(calls) == 1


def test_gsc_url_inspection_is_stored_index_evidence_and_property_scoped():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}})
    provider = GSCClient("sc-domain:example.com", client=client(handler), token_provider=lambda: "fixture")
    result = provider.inspect_url("https://www.example.com/windows/")
    assert result.metadata["live_test"] is False
    assert result.rows[0]["indexStatusResult"]["verdict"] == "PASS"
    with pytest.raises(ValueError):
        provider.inspect_url("https://example.com.attacker.com/")
    assert len(calls) == 1
