"""Qualified outcomes use a separate, verified, bounded GA4 report."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json

import httpx
import pytest
from sqlalchemy import select

from backend.app.contracts import stable_hash
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.integrations.common import MalformedResponse, ProviderError
from backend.app.integrations.google_analytics import GA4Client
from backend.app.integrations.google_analytics import client as ga4_module
from backend.app.services.control import ingest_batch
from backend.app.services.measurement import _window


START = date(2026, 8, 1)
DIMENSIONS = ("date", "landingPage", "sessionDefaultChannelGroup")


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ga4_module, "utcnow", lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc))


def definition(**changes):
    return {
        "verified": True, "tracking_verified": True, "qualification_verified": True,
        "deduplication_verified": True, "qualified_events": ["qualified_form", "qualified_call"],
        "qualification_definition": "CRM-accepted leads for a service we sell",
        "deduplication_method": "One event per CRM lead ID across mutually exclusive event names",
        "value_method": "fixed_per_qualified_conversion", "value_per_conversion": 40,
        "currency": "USD",
    } | changes


def raw_row(*values, event=None, day="20260801", landing="/services", channel="Organic Search"):
    dimensions = [day, landing, channel] + ([event] if event is not None else [])
    return {"dimensionValues": [{"value": value} for value in dimensions],
            "metricValues": [{"value": str(value)} for value in values]}


def provider(*, session_rows=None, event_rows=None, session_metadata=None, event_metadata=None,
             event_value=False, transform=None, on_request=None):
    session_rows = [raw_row(100, 91)] if session_rows is None else session_rows
    event_rows = [raw_row(2, 110, event="qualified_form")] if event_value and event_rows is None else event_rows
    event_rows = [raw_row(2, event="qualified_form")] if event_rows is None else event_rows
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        operation = request.url.path.rsplit(":", 1)[1]
        requests.append((operation, payload))
        if on_request is not None:
            on_request(operation, payload)
        if operation == "checkCompatibility":
            metrics = ("eventCount", "eventValue") if event_value else ("eventCount",)
            data = {
                "dimensionCompatibilities": [{"dimensionMetadata": {"apiName": name}, "compatibility": "COMPATIBLE"}
                                             for name in (*DIMENSIONS, "eventName")],
                "metricCompatibilities": [{"metricMetadata": {"apiName": name}, "compatibility": "COMPATIBLE"}
                                          for name in metrics],
            }
            kind, offset = "compatibility", 0
        else:
            assert operation == "runReport"
            is_qualified = payload["metrics"][0]["name"] == "eventCount"
            dimensions = (*DIMENSIONS, "eventName") if is_qualified else DIMENSIONS
            metrics = (("eventCount", "eventValue") if event_value else ("eventCount",)) if is_qualified else ("sessions", "keyEvents")
            rows = event_rows if is_qualified else session_rows
            metadata = event_metadata if is_qualified else session_metadata
            offset, limit = int(payload["offset"]), int(payload["limit"])
            data = {
                "dimensionHeaders": [{"name": name} for name in dimensions],
                "metricHeaders": [{"name": name} for name in metrics],
                "rowCount": len(rows), "rows": deepcopy(rows[offset:offset + limit]),
                "metadata": {"timeZone": "UTC", "currencyCode": "USD"} | (metadata or {}),
            }
            kind = "qualified" if is_qualified else "sessions"
        if transform is not None:
            transform(kind, offset, data)
        return httpx.Response(200, json=data)

    return GA4Client("123", token_provider=lambda: "mock-token", client=httpx.Client(transport=httpx.MockTransport(handler))), requests


@pytest.mark.parametrize("mapping", [None, {}])
def test_unconfigured_fetch_preserves_sessions_key_events_and_unknown_outcomes(mapping):
    client, requests = provider()
    batch = client.fetch(START, START, conversion_definition=mapping)
    assert len(requests) == 1 and requests[0][0] == "runReport"
    assert batch.rows[0].sessions == 100 and batch.rows[0].key_events == 91
    assert batch.rows[0].qualified_conversions is None and batch.rows[0].conversion_value is None
    assert batch.complete is False and batch.metadata["complete_dates"] == []
    assert batch.metadata["qualified_conversion_semantics_verified"] is False
    assert "business_conversion_definition_unconfirmed" in batch.quality_flags


def test_separate_paginated_event_report_never_inflates_sessions():
    mapping = definition()
    client, requests = provider(
        session_rows=[raw_row(100, 91), raw_row(80, 75, landing="/other")],
        event_rows=[raw_row(2, event="qualified_form"), raw_row(3, event="qualified_call"),
                    raw_row(4, event="qualified_form", landing="/other")],
    )
    batch = client.fetch(START, START, page_size=1, conversion_definition=mapping)
    assert [(row.landing_page, row.sessions, row.key_events, row.qualified_conversions, row.conversion_value)
            for row in batch.rows] == [("/services", 100, 91, 5, 200), ("/other", 80, 75, 4, 160)]
    assert sum(row.sessions for row in batch.rows) == 180
    compatible = requests[0][1]
    session_requests = [payload for operation, payload in requests if operation == "runReport" and payload["metrics"][0]["name"] == "sessions"]
    event_requests = [payload for operation, payload in requests if operation == "runReport" and payload["metrics"][0]["name"] == "eventCount"]
    assert [payload["offset"] for payload in session_requests] == ["0", "1"]
    assert [payload["offset"] for payload in event_requests] == ["0", "1", "2"]
    assert all(payload["dimensions"] == [{"name": name} for name in DIMENSIONS] for payload in session_requests)
    assert event_requests[0]["metrics"] == [{"name": "eventCount"}]
    assert event_requests[0]["dimensions"] == [{"name": name} for name in (*DIMENSIONS, "eventName")]
    assert event_requests[0]["dimensionFilter"] == {"andGroup": {"expressions": [
        {"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {
            "matchType": "EXACT", "value": "Organic Search", "caseSensitive": True}}},
        {"filter": {"fieldName": "eventName", "inListFilter": {
            "values": mapping["qualified_events"], "caseSensitive": True}}},
    ]}}
    for field in ("dimensions", "metrics", "dimensionFilter"):
        assert compatible[field] == event_requests[0][field]
    assert batch.complete is True
    assert batch.metadata["complete_dates"] == ["2026-08-01"]
    assert batch.metadata["scope"] == "all_organic_landing_pages"
    assert batch.metadata["conversion_definition_hash"] == stable_hash(mapping)
    assert batch.metadata["tracking_verified"] is True
    assert batch.metadata["qualified_conversion_semantics_verified"] is True
    assert batch.metadata["qualified_conversion_value_semantics_verified"] is True
    assert batch.metadata["conversion_value_kind"] == "modeled_lead_value"


def test_verified_event_values_use_only_selected_events_and_explicit_currency():
    client, requests = provider(event_value=True, event_rows=[
        raw_row(2, 110.50, event="qualified_form"), raw_row(3, 90, event="qualified_call"),
    ])
    batch = client.fetch(START, START, conversion_definition=definition(
        value_method="event_value", currency_verified=True, value_semantics_verified=True))
    row = batch.rows[0]
    assert (row.sessions, row.key_events, row.qualified_conversions, row.conversion_value) == (100, 91, 5, 200.5)
    assert requests[-1][1]["metrics"] == [{"name": "eventCount"}, {"name": "eventValue"}]
    assert requests[-1][1]["currencyCode"] == "USD"
    assert batch.metadata["conversion_value_kind"] == "verified_event_value"
    assert batch.metadata["currency"] == "USD"
    assert "modeled_lead_value" not in batch.quality_flags


@pytest.mark.parametrize("field", ["verified", "tracking_verified", "qualification_verified", "deduplication_verified"])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_attestations_require_exact_true_before_http(field, value):
    client, requests = provider()
    with pytest.raises(ValueError, match=field):
        client.fetch(START, START, conversion_definition=definition(**{field: value}))
    assert requests == []


@pytest.mark.parametrize("changes", [
    {"qualified_events": []}, {"qualified_events": "qualified_form"}, {"qualified_events": [""]},
    {"qualified_events": ["qualified_form", "qualified_form"]}, {"qualified_events": [{}]},
    {"qualification_definition": " "}, {"deduplication_method": None},
    {"value_method": "totalRevenue"}, {"currency": "usd"}, {"currency": None},
    {"value_per_conversion": -1}, {"value_per_conversion": float("inf")},
    {"value_per_conversion": float("nan")}, {"value_per_conversion": True},
    {"value_per_conversion": "40"}, {"value_per_conversion": 10 ** 400},
    {"value_method": "event_value", "currency_verified": True},
    {"value_method": "event_value", "value_semantics_verified": True, "currency_verified": "true"},
    {"value_method": "event_value", "value_semantics_verified": 1, "currency_verified": True},
])
def test_incomplete_or_ambiguous_definitions_fail_before_http(changes):
    client, requests = provider()
    with pytest.raises(ValueError):
        client.fetch(START, START, conversion_definition=definition(**changes))
    assert requests == []


@pytest.mark.parametrize("mapping", [[], "qualified_form", True])
def test_definition_must_be_a_dictionary(mapping):
    client, requests = provider()
    with pytest.raises(ValueError):
        client.fetch(START, START, conversion_definition=mapping)
    assert requests == []


def test_missing_qualified_page_date_remains_unknown_and_invalidates_that_date():
    client, _ = provider(session_rows=[raw_row(100, 91), raw_row(80, 75, day="20260802")])
    batch = client.fetch(START, "2026-08-02", conversion_definition=definition())
    assert batch.rows[0].qualified_conversions == 2
    assert batch.rows[1].qualified_conversions is None and batch.rows[1].conversion_value is None
    assert batch.metadata["complete_dates"] == ["2026-08-01"]
    assert batch.metadata["missing_qualified_row_count"] == 1
    assert batch.complete is False and "qualified_outcome_missing" in batch.quality_flags


def test_one_missing_page_prevents_whole_date_completeness():
    client, _ = provider(session_rows=[raw_row(100, 91), raw_row(80, 75, landing="/other")])
    batch = client.fetch(START, START, conversion_definition=definition())
    assert batch.rows[1].qualified_conversions is None
    assert batch.metadata["complete_dates"] == [] and batch.complete is False


def test_absent_qualified_report_is_not_invented_zero():
    client, _ = provider(event_rows=[])
    batch = client.fetch(START, START, conversion_definition=definition())
    assert batch.rows[0].qualified_conversions is None and batch.rows[0].conversion_value is None
    assert batch.metadata["qualified_pagination_exhausted"] is True
    assert batch.metadata["complete_dates"] == [] and batch.complete is False


def test_explicit_zero_qualified_observation_and_zero_modeled_value_are_retained():
    client, _ = provider(event_rows=[raw_row(0, event="qualified_form")])
    batch = client.fetch(START, START, conversion_definition=definition(value_per_conversion=0))
    assert batch.rows[0].qualified_conversions == batch.rows[0].conversion_value == 0
    assert batch.complete is True


def test_missing_dates_are_unknown_even_after_full_pagination():
    client, _ = provider()
    batch = client.fetch(START, "2026-08-02", conversion_definition=definition())
    assert batch.metadata["missing_dates"] == ["2026-08-02"]
    assert batch.metadata["complete_dates"] == ["2026-08-01"]
    assert batch.complete is False and "missing_dates" in batch.quality_flags


@pytest.mark.parametrize("kind", ["sessions", "qualified"])
@pytest.mark.parametrize(("metadata", "original", "normalized"), [
    ({"subjectToThresholding": True}, "subjectToThresholding", "thresholding"),
    ({"dataLossFromOtherRow": True}, "dataLossFromOtherRow", "aggregated_other_rows"),
    ({"samplingMetadatas": [{"samplesReadCount": "10", "samplingSpaceSize": "100"}]}, "samplingMetadatas", "sampled_report"),
])
def test_privacy_flags_from_either_report_are_preserved_and_block_outcomes(kind, metadata, original, normalized):
    client, _ = provider(**{("session_metadata" if kind == "sessions" else "event_metadata"): metadata})
    batch = client.fetch(START, START, conversion_definition=definition())
    assert {original, normalized} <= set(batch.quality_flags)
    assert {original, normalized} <= set(batch.rows[0].quality_flags)
    assert batch.rows[0].sessions == 100 and batch.rows[0].qualified_conversions is None
    assert batch.complete is False and batch.metadata["complete_dates"] == []
    metadata_key = "report_metadata" if kind == "sessions" else "qualified_report_metadata"
    assert batch.metadata[metadata_key][0][original] == metadata[original]


def test_qualified_row_budget_never_publishes_partial_event_sum():
    client, requests = provider(event_rows=[raw_row(2, event="qualified_form"), raw_row(3, event="qualified_call")])
    batch = client.fetch(START, START, max_rows=1, page_size=1, conversion_definition=definition())
    assert len(requests) == 3
    assert batch.rows[0].sessions == 100 and batch.rows[0].qualified_conversions is None
    assert batch.metadata["qualified_pagination_exhausted"] is False
    assert batch.metadata["pagination_exhausted"] is False
    assert batch.metadata["complete_dates"] == []
    assert "row_budget_reached" in batch.quality_flags


def test_session_row_budget_blocks_qualified_coverage():
    client, _ = provider(session_rows=[raw_row(100, 91), raw_row(50, 45, landing="/other")])
    batch = client.fetch(START, START, max_rows=1, conversion_definition=definition())
    assert batch.rows[0].sessions == 100 and batch.rows[0].qualified_conversions is None
    assert batch.complete is False and batch.metadata["sessions_pagination_exhausted"] is False


def test_orphan_qualified_row_does_not_create_or_add_sessions():
    client, _ = provider(event_rows=[raw_row(2, event="qualified_form", landing="/missing")])
    batch = client.fetch(START, START, conversion_definition=definition())
    assert len(batch.rows) == 1 and batch.rows[0].sessions == 100
    assert batch.rows[0].qualified_conversions is None
    assert batch.metadata["unmatched_qualified_row_count"] == 1
    assert "api_disagreement" in batch.quality_flags and batch.complete is False


@pytest.mark.parametrize("kind", ["sessions", "qualified"])
def test_nonorganic_rows_are_rejected_even_if_the_provider_ignores_filter(kind):
    data = [raw_row(2, event="qualified_form", channel="Paid Search")] if kind == "qualified" else [raw_row(100, 91, channel="Paid Search")]
    client, _ = provider(**{("event_rows" if kind == "qualified" else "session_rows"): data})
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, conversion_definition=definition())


@pytest.mark.parametrize("row", [
    raw_row(2, event="page_view"), raw_row(-1, event="qualified_form"),
    raw_row(1.5, event="qualified_form"), raw_row("NaN", event="qualified_form"),
    raw_row(2, event="qualified_form", day="20260901"), raw_row(2, event="qualified_form", day="2026081"),
    {"dimensionValues": [{"value": "20260801"}], "metricValues": [{"value": "2"}]},
])
def test_malformed_qualified_rows_fail_closed(row):
    client, _ = provider(event_rows=[row])
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, conversion_definition=definition())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "-Infinity"])
def test_nonfinite_or_negative_event_value_is_not_an_observation(value):
    client, _ = provider(event_value=True, event_rows=[raw_row(2, value, event="qualified_form")])
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, conversion_definition=definition(
            value_method="event_value", currency_verified=True, value_semantics_verified=True))


def test_nonfinite_legacy_key_event_value_is_rejected():
    client, _ = provider(session_rows=[raw_row(100, "Infinity")])
    with pytest.raises(MalformedResponse):
        client.fetch(START, START)


def test_currency_mismatch_rejects_event_value_report():
    client, _ = provider(event_value=True, event_metadata={"currencyCode": "EUR"})
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, conversion_definition=definition(
            value_method="event_value", currency_verified=True, value_semantics_verified=True))


@pytest.mark.parametrize("failure", ["repeated", "drift", "stalled", "headers", "privacy_type", "row_count"])
def test_inconsistent_qualified_report_does_not_yield_a_batch(failure):
    first, second = raw_row(2, event="qualified_form"), raw_row(3, event="qualified_call")

    def transform(kind, offset, data):
        if kind != "qualified":
            return
        if failure == "repeated" and offset:
            data["rows"] = [first]
        if failure == "drift" and offset:
            data["rowCount"] = 3
        if failure == "stalled" and offset:
            data["rows"] = []
        if failure == "headers":
            data["metricHeaders"] = [{"name": "sessions"}]
        if failure == "privacy_type":
            data["metadata"]["subjectToThresholding"] = "false"
        if failure == "row_count":
            data["rowCount"] = 1.5

    client, _ = provider(event_rows=[first, second], transform=transform)
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, page_size=1, conversion_definition=definition())


@pytest.mark.parametrize("failure", ["missing", "incompatible", "duplicate", "malformed"])
def test_compatibility_must_explicitly_confirm_all_requested_fields(failure):
    def transform(kind, offset, data):
        if kind != "compatibility":
            return
        entries = data["dimensionCompatibilities"]
        if failure == "missing":
            entries.pop()
        elif failure == "incompatible":
            entries[-1]["compatibility"] = "INCOMPATIBLE"
        elif failure == "duplicate":
            entries.append(deepcopy(entries[0]))
        else:
            data["metricCompatibilities"] = None

    client, requests = provider(transform=transform)
    with pytest.raises(ProviderError):
        client.fetch(START, START, conversion_definition=definition())
    assert [operation for operation, _ in requests] == ["checkCompatibility"]


def test_definition_is_snapshotted_before_requests_and_hash_changes_on_next_fetch():
    mapping = definition()
    original_hash = stable_hash(mapping)

    def mutate_definition(operation, payload):
        if operation == "checkCompatibility":
            mapping["value_per_conversion"] = 100

    client, _ = provider(on_request=mutate_definition)
    first = client.fetch(START, START, conversion_definition=mapping)
    second = client.fetch(START, START, conversion_definition=mapping)
    assert first.rows[0].conversion_value == 80 and first.metadata["conversion_definition_hash"] == original_hash
    assert second.rows[0].conversion_value == 200
    assert second.metadata["conversion_definition_hash"] == stable_hash(mapping) != original_hash


def test_overflowed_modeled_value_fails_closed():
    client, _ = provider(event_rows=[raw_row(2, event="qualified_form")])
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, conversion_definition=definition(value_per_conversion=1e308))


def test_overflowed_integer_product_fails_as_provider_error():
    client, _ = provider(event_rows=[raw_row(10 ** 100, event="qualified_form")])
    with pytest.raises(MalformedResponse):
        client.fetch(START, START, conversion_definition=definition(value_per_conversion=10 ** 300))


def test_freshness_holdback_excludes_recent_dates_without_contaminating_historical_rows():
    client, _ = provider(
        session_rows=[raw_row(100, 91, day="20260819"), raw_row(80, 75, day="20260820")],
        event_rows=[raw_row(2, event="qualified_form", day="20260819"),
                    raw_row(3, event="qualified_form", day="20260820")],
    )
    batch = client.fetch("2026-08-19", "2026-08-20", conversion_definition=definition())
    assert batch.metadata["report_timezone"] == "UTC"
    assert batch.metadata["extraction_complete"] is True
    assert batch.metadata["extraction_complete_dates"] == ["2026-08-19", "2026-08-20"]
    assert batch.metadata["complete_dates"] == ["2026-08-19"] and batch.complete is False
    assert batch.metadata["outcome_holdback_days"] == 12
    assert batch.metadata["held_back_dates"] == ["2026-08-20"]
    assert batch.metadata["data_finality_guaranteed"] is False
    assert "data_not_final" not in batch.quality_flags
    assert "data_not_final" not in batch.rows[0].quality_flags
    assert "data_not_final" in batch.rows[1].quality_flags
    assert batch.rows[1].qualified_conversions == 3  # Preserved as a provisional observation.


def test_configured_holdback_uses_property_calendar_not_server_utc(monkeypatch):
    monkeypatch.setattr(ga4_module, "utcnow", lambda: datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc))
    client, _ = provider(
        session_rows=[raw_row(100, 91, day="20260831")],
        event_rows=[raw_row(2, event="qualified_form", day="20260831")],
        session_metadata={"timeZone": "America/Los_Angeles"},
        event_metadata={"timeZone": "America/Los_Angeles"},
    )
    batch = client.fetch("2026-08-31", "2026-08-31", conversion_definition=definition(), outcome_holdback_days=0)
    assert batch.metadata["freshness_cutoff_exclusive"] == "2026-08-31"
    assert batch.metadata["complete_dates"] == [] and batch.complete is False
    assert "data_not_final" in batch.rows[0].quality_flags


def test_configurable_holdback_can_include_recent_closed_dates_but_never_today():
    client, _ = provider(
        session_rows=[raw_row(100, 91, day="20260831"), raw_row(80, 75, day="20260901")],
        event_rows=[raw_row(2, event="qualified_form", day="20260831"),
                    raw_row(3, event="qualified_form", day="20260901")],
    )
    batch = client.fetch("2026-08-31", "2026-09-01", conversion_definition=definition(), outcome_holdback_days=0)
    assert batch.metadata["complete_dates"] == ["2026-08-31"]
    assert "data_not_final" not in batch.rows[0].quality_flags
    assert "data_not_final" in batch.rows[1].quality_flags


@pytest.mark.parametrize("holdback", [-1, 366, False, 1.5])
def test_invalid_holdback_is_rejected_before_http(holdback):
    client, requests = provider()
    with pytest.raises(ValueError, match="outcome_holdback_days"):
        client.fetch(START, START, conversion_definition=definition(), outcome_holdback_days=holdback)
    assert requests == []


@pytest.mark.parametrize(("zone", "flag"), [
    (None, "report_timezone_unconfirmed"), ("Invalid/Timezone", "report_timezone_unconfirmed"),
    (42, "report_timezone_unconfirmed"), ("Europe/London", "timezone_disagreement"),
])
def test_missing_invalid_or_inconsistent_report_timezone_prevents_coverage(zone, flag):
    client, _ = provider(event_metadata={"timeZone": zone})
    batch = client.fetch(START, START, conversion_definition=definition())
    assert batch.metadata["report_timezone_verified"] is False
    assert batch.rows[0].qualified_conversions is None
    assert batch.metadata["complete_dates"] == [] and batch.complete is False
    assert flag in batch.quality_flags


def test_ingested_metadata_is_accepted_by_measurement_and_definition_changes_invalidate_it():
    mapping = definition()
    client, _ = provider()
    batch = client.fetch(START, START, conversion_definition=mapping)
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            site = m.Site(name="Qualified measurement", base_url="https://example.com",
                          conversion_definition=mapping, config_json={"source_mode": "live"})
            session.add(site)
            session.flush()
            evidence_id = ingest_batch(session, site, "ga4", batch)
            evidence = session.get(m.Evidence, evidence_id)
            page = session.scalar(select(m.Page).where(m.Page.site_id == site.id))
            window, provenance = _window(session, site, [page], START, START, [evidence])
            assert window.collection_complete is True and window.tracking_complete is True
            assert (window.sessions, window.qualified_conversions, window.qualified_conversion_value) == (100, 2, 80)
            assert provenance["qualification_verified"] is True and provenance["value_verified"] is True
            site.conversion_definition = definition(value_per_conversion=100)
            changed, provenance = _window(session, site, [page], START, START, [evidence])
            assert changed.qualified_conversions is None and changed.qualified_conversion_value is None
            assert provenance["conversion_definition_matches"] is False
            assert "conversion_definition_changed" in changed.quality_flags
    finally:
        engine.dispose()


def test_ingested_recent_rows_do_not_block_a_separate_mature_measurement_window():
    client, _ = provider(
        session_rows=[raw_row(100, 91, day="20260819"), raw_row(80, 75, day="20260820")],
        event_rows=[raw_row(2, event="qualified_form", day="20260819"),
                    raw_row(3, event="qualified_form", day="20260820")],
    )
    mapping = definition()
    batch = client.fetch("2026-08-19", "2026-08-20", conversion_definition=mapping)
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            site = m.Site(name="Mixed freshness", base_url="https://example.com",
                          conversion_definition=mapping, config_json={"source_mode": "live"})
            session.add(site)
            session.flush()
            evidence_id = ingest_batch(session, site, "ga4", batch)
            evidence = session.get(m.Evidence, evidence_id)
            page = session.scalar(select(m.Page).where(m.Page.site_id == site.id))
            mature_date, recent_date = date(2026, 8, 19), date(2026, 8, 20)
            mature, _ = _window(session, site, [page], mature_date, mature_date, [evidence])
            recent, _ = _window(session, site, [page], recent_date, recent_date, [evidence])
            assert mature.collection_complete is True
            assert mature.qualified_conversion_value == 80
            assert "data_not_final" not in mature.quality_flags
            assert recent.collection_complete is False
            assert "data_not_final" in recent.quality_flags
    finally:
        engine.dispose()
