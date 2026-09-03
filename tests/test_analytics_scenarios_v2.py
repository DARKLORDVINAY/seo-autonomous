"""Protocol-to-database analytics scenarios, never real Google observations.

Every request is intercepted by MockTransport. Saved batches have explicit
fixture provenance, use isolated databases, and cannot certify business success.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import func, select

from backend.app.contracts import ProviderUnavailable
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.experiments.evaluation import ExperimentSpec, evaluate_experiment
from backend.app.integrations import common
from backend.app.integrations.common import MalformedResponse, ProviderError
from backend.app.integrations.google_analytics import GA4Client
from backend.app.integrations.google_analytics import client as ga4_module
from backend.app.integrations.google_search_console import GSCClient
from backend.app.integrations.google_search_console import client as gsc_module
from backend.app.seo.analysis import AnalysisContext, data_quality_report, detect_content_decay
from backend.app.services.control import ingest_batch
from backend.app.services.measurement import _window


START = date(2026, 6, 1)
NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
ORIGIN = "https://analytics-scenarios.test"


def definition():
    """Deliberately fictional verified semantics, only inside fixture sites."""
    return {
        "verified": True, "tracking_verified": True, "qualification_verified": True,
        "deduplication_verified": True, "qualified_events": ["fixture_qualified_event"],
        "qualification_definition": "Synthetic accepted outcome; not a real customer or business claim",
        "deduplication_method": "Fixture scenario emits one event per synthetic outcome",
        "value_method": "fixed_per_qualified_conversion", "value_per_conversion": 10,
        "currency": "GBP",
    }


def ga4_row(sessions_or_events, *, day=START, landing="/guide", qualified=False):
    dimensions = [day.strftime("%Y%m%d"), landing, "Organic Search"]
    if qualified:
        dimensions.append("fixture_qualified_event")
    metrics = [sessions_or_events] if qualified else [sessions_or_events, 0]
    return {"dimensionValues": [{"value": value} for value in dimensions],
            "metricValues": [{"value": str(value)} for value in metrics]}


def gsc_row(*, day=START, page="/guide", clicks=10, impressions=100, position=8):
    return {"keys": [day.isoformat(), ORIGIN + page, "fixture guide"],
            "clicks": clicks, "impressions": impressions, "position": position}


class GoogleScript:
    """Stateful, bounded fake external protocol; production clients parse it."""

    def __init__(self, *, sessions=(), outcomes=(), gsc=(), zone="Europe/London", transform=None):
        self.sessions, self.outcomes, self.gsc = list(sessions), list(outcomes), list(gsc)
        self.zone, self.transform, self.requests = zone, transform, []
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle), trust_env=False)

    def close(self):
        self.http.close()

    def handle(self, request):
        assert request.url.host in {"analyticsdata.googleapis.com", "www.googleapis.com"}
        assert request.method == "POST"
        payload = json.loads(request.content)
        if request.url.host == "www.googleapis.com":
            kind, offset = "gsc", payload["startRow"]
            rows = self.gsc[offset:offset + payload["rowLimit"]]
            data = {"rows": deepcopy(rows)} if rows else {}
        elif request.url.path.endswith(":checkCompatibility"):
            kind, offset = "compatibility", 0
            data = {
                "dimensionCompatibilities": [{"dimensionMetadata": {"apiName": item["name"]},
                                                "compatibility": "COMPATIBLE"}
                                               for item in payload["dimensions"]],
                "metricCompatibilities": [{"metricMetadata": {"apiName": item["name"]},
                                             "compatibility": "COMPATIBLE"}
                                            for item in payload["metrics"]],
            }
        else:
            assert request.url.path.endswith(":runReport")
            kind = "qualified" if payload["metrics"][0]["name"] == "eventCount" else "sessions"
            rows = self.outcomes if kind == "qualified" else self.sessions
            offset, limit = int(payload["offset"]), int(payload["limit"])
            data = {"dimensionHeaders": payload["dimensions"], "metricHeaders": payload["metrics"],
                    "rowCount": len(rows), "rows": deepcopy(rows[offset:offset + limit]),
                    "metadata": {"timeZone": self.zone, "currencyCode": "GBP"}}
        self.requests.append({"kind": kind, "offset": offset, "payload": payload})
        if self.transform:
            override = self.transform(kind, offset, data)
            if override is not None:
                return override
        return httpx.Response(200, json=data)

    def analytics(self, start=START, end=START, **kwargs):
        return GA4Client("987654321", client=self.http, token_provider=lambda: "fixture-not-a-secret").fetch(
            start, end, conversion_definition=definition(), **kwargs)

    def search(self, start=START, end=START, **kwargs):
        return GSCClient(ORIGIN, client=self.http, token_provider=lambda: "fixture-not-a-secret").fetch(start, end, **kwargs)


def fixture_batch(batch, scenario, *, fetched_at=NOW):
    """An injected MockTransport does not make the real client self-label a fixture."""
    result = deepcopy(batch)
    result.source = f"fixture:analytics-v2:{batch.source}"
    result.fetched_at = fetched_at
    result.quality_flags = sorted(set(result.quality_flags) | {"fixture_data"})
    result.metadata.update({"fixture_scenario": scenario, "business": "fictional",
                            "calibration_eligible": False, "qualifies_for_autonomy": False,
                            "external_calls": 0, "paid_api_calls": 0})
    return result


@pytest.fixture(autouse=True)
def fixture_clock_and_no_retry_sleep(monkeypatch):
    monkeypatch.setattr(ga4_module, "utcnow", lambda: NOW)
    for module in (ga4_module, gsc_module):
        monkeypatch.setattr(module, "request", lambda *args, **kwargs: common.request(
            *args, sleep=lambda seconds: None, **kwargs))


@pytest.fixture
def script():
    scripts = []

    def create(**kwargs):
        created = GoogleScript(**kwargs)
        scripts.append(created)
        return created

    yield create
    for item in scripts:
        item.close()


@pytest.fixture
def db():
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = m.Site(name="Synthetic analytics scenarios", base_url=ORIGIN, autonomy_level=1,
                      production_enabled=False, conversion_definition=definition(),
                      config_json={"source_mode": "fixture", "max_daily_actions": 0,
                                   "earned_autonomous_categories": []})
        session.add(site)
        session.flush()
        yield session, site
    engine.dispose()


def ingest(session, site, batch, *, scenario="v2", fetched_at=NOW):
    identifier = ingest_batch(session, site, "ga4", fixture_batch(batch, scenario, fetched_at=fetched_at))
    return session.get(m.Evidence, identifier)


@pytest.mark.parametrize("kind", ["gsc", "ga4"])
def test_explicit_zero_is_distinct_from_an_empty_successful_report(kind, script):
    missing = script()
    if kind == "gsc":
        empty = missing.search()
        observed = script(gsc=[gsc_row(clicks=0, impressions=0, position=0)]).search()
        assert observed.rows[0].clicks == observed.rows[0].impressions == 0
        assert observed.complete is False  # Final rows are not exhaustive coverage.
    else:
        empty = missing.analytics()
        observed = script(sessions=[ga4_row(0)], outcomes=[ga4_row(0, qualified=True)]).analytics()
        assert observed.rows[0].sessions == observed.rows[0].qualified_conversions == 0
        assert observed.rows[0].conversion_value == 0
        assert observed.complete is True
    assert empty.rows == [] and empty.complete is False
    assert empty.metadata["missing_dates"] == [START.isoformat()]
    assert "omitted_dates_are_unknown" in empty.quality_flags


@pytest.mark.parametrize("kind", ["gsc", "ga4"])
@pytest.mark.parametrize("status", [403, 429, 503])
def test_unavailable_provider_never_returns_empty_or_zero_batch(kind, status, script, db):
    session, site = db
    fake = script(transform=lambda *args: httpx.Response(status, headers={"Retry-After": "0"}))
    with pytest.raises((ProviderError, ProviderUnavailable)):
        fake.search() if kind == "gsc" else fake.analytics()
    assert len(fake.requests) == (1 if status == 403 else 3)
    assert session.scalar(select(func.count(m.Evidence.id))) == 0
    assert session.scalar(select(func.count(m.GA4Daily.id))) == 0
    assert site.autonomy_level == 1 and site.production_enabled is False


@pytest.mark.parametrize("fault", ["repeated", "contradictory", "late_timeout"])
@pytest.mark.parametrize("kind", ["gsc", "ga4"])
def test_second_page_failures_do_not_release_a_partial_batch(kind, fault, script):
    first_gsc, first_ga4 = gsc_row(), ga4_row(100)

    def fault_page(report, offset, data):
        if offset != 1 or report != ("gsc" if kind == "gsc" else "sessions"):
            return None
        if fault == "late_timeout":
            raise httpx.ReadTimeout("simulated page-two timeout")
        row = deepcopy(first_gsc if kind == "gsc" else first_ga4)
        if fault == "contradictory":
            if kind == "gsc":
                row["clicks"] = 20
            else:
                row["metricValues"][0]["value"] = "200"
        data["rows"] = [row]

    fake = script(gsc=[first_gsc, gsc_row(page="/other")],
                  sessions=[first_ga4, ga4_row(50, landing="/other")], transform=fault_page)
    with pytest.raises(ProviderError):
        fake.search(page_size=1) if kind == "gsc" else fake.analytics(page_size=1)
    assert len(fake.requests) <= 5  # Includes GA4 compatibility, never unbounded retries.


@pytest.mark.parametrize("metadata", [
    {"first_incomplete_date": "2026-06-01"},
    {"first_incomplete_date": "2026-05-31"},
    {"first_incomplete_date": "not-a-date"},
])
def test_gsc_final_request_cannot_promote_contradictory_incomplete_metadata(metadata, script):
    def partial(kind, offset, data):
        data["metadata"] = metadata

    fake = script(gsc=[gsc_row()], transform=partial)
    with pytest.raises(MalformedResponse):
        fake.search()
    assert all(item["payload"]["dataState"] == "final" for item in fake.requests)


def test_gsc_incomplete_date_outside_requested_window_is_preserved_not_misclassified(script):
    def outside_window(kind, offset, data):
        data["metadata"] = {"first_incomplete_date": (START + timedelta(days=1)).isoformat()}

    batch = script(gsc=[gsc_row()], transform=outside_window).search()
    assert batch.rows[0].data_state == "final"
    assert batch.metadata["report_metadata"] == [{"first_incomplete_date": "2026-06-02"}]
    assert batch.complete is False  # Exhausted pagination is still not a complete GSC dataset.


@pytest.mark.parametrize("changes", [
    {"clicks": 101, "impressions": 100}, {"clicks": True}, {"impressions": False},
    {"position": True}, {"position": "NaN"}, {"position": "Infinity"},
])
def test_gsc_inconsistent_metrics_rejected_before_canonical_ingestion(changes, script):
    with pytest.raises(MalformedResponse):
        script(gsc=[gsc_row(**changes)]).search()


def test_gsc_budget_and_suppression_never_certify_a_decline(script):
    rows = [gsc_row(day=START + timedelta(days=i), clicks=20 if i < 28 else 5) for i in range(56)]
    batch = script(gsc=rows).search(START, START + timedelta(days=55), max_rows=55, page_size=11)
    assert batch.complete is False and "row_budget_reached" in batch.quality_flags
    assert batch.metadata["full_dataset_guaranteed"] is False
    assert batch.metadata["missing_dates"] == [(START + timedelta(days=55)).isoformat()]
    assert detect_content_decay(batch.rows, AnalysisContext(site_url=ORIGIN)) == []
    assert "gsc_dates_are_pacific_time" in batch.quality_flags


@pytest.mark.parametrize(("now", "zone", "cutoff"), [
    ("2026-03-29T00:30:00+00:00", "Europe/London", "2026-03-29"),
    ("2026-03-29T23:30:00+00:00", "Europe/London", "2026-03-30"),
    ("2026-10-25T00:30:00+00:00", "Europe/London", "2026-10-25"),
    ("2026-10-25T23:30:00+00:00", "Europe/London", "2026-10-25"),
    ("2026-03-09T00:30:00+00:00", "America/Los_Angeles", "2026-03-08"),
    ("2026-11-02T00:30:00+00:00", "America/Los_Angeles", "2026-11-01"),
])
def test_property_calendar_dst_cutoff_not_24_hour_or_utc_guess(now, zone, cutoff, monkeypatch, script):
    clock = datetime.fromisoformat(now)
    monkeypatch.setattr(ga4_module, "utcnow", lambda: clock)
    current = date.fromisoformat(cutoff)
    prior = current - timedelta(days=1)
    fake = script(zone=zone, sessions=[ga4_row(100, day=prior), ga4_row(100, day=current)],
                  outcomes=[ga4_row(2, day=prior, qualified=True), ga4_row(2, day=current, qualified=True)])
    batch = fake.analytics(prior, current, outcome_holdback_days=0)
    assert batch.metadata["freshness_cutoff_exclusive"] == cutoff
    assert batch.metadata["complete_dates"] == [prior.isoformat()]
    assert "data_not_final" not in batch.rows[0].quality_flags
    assert "data_not_final" in batch.rows[1].quality_flags
    assert batch.metadata["data_finality_guaranteed"] is False


def test_delayed_qualification_and_late_revision_keep_evidence_and_one_daily_grain(script, db):
    session, site = db
    fake = script(sessions=[ga4_row(100)])
    missing = ingest(session, site, fake.analytics(), scenario="qualification-not-arrived")
    page = session.scalar(select(m.Page))
    window, _ = _window(session, site, [page], START, START, [missing])
    assert window.qualified_conversions is None and window.collection_complete is False
    fake.outcomes = [ga4_row(2, qualified=True)]
    arrived = ingest(session, site, fake.analytics(), scenario="qualification-arrived",
                     fetched_at=NOW + timedelta(minutes=1))
    first, _ = _window(session, site, [page], START, START, [arrived, missing])
    assert first.qualified_conversions == 2
    fake.outcomes = [ga4_row(3, qualified=True)]
    revised = ingest(session, site, fake.analytics(), scenario="attribution-backfill",
                     fetched_at=NOW + timedelta(minutes=2))
    last, provenance = _window(session, site, [page], START, START, [revised, arrived, missing])
    assert last.qualified_conversions == 3 and last.qualified_conversion_value == 30
    assert provenance["evidence_ids"] == [revised.id]
    assert missing.content["rows"][0]["qualified_conversions"] is None
    assert arrived.content["rows"][0]["qualified_conversions"] == 2
    assert session.scalar(select(func.count(m.GA4Daily.id))) == 1
    assert session.scalar(select(func.count(m.CalibrationRecord.id))) == 0
    assert "fixture_outcomes_cannot_earn_production_autonomy" in last.quality_flags


def test_new_report_omits_page_old_database_row_must_not_become_current(script, db):
    session, site = db
    fake = script(sessions=[ga4_row(100), ga4_row(80, landing="/other")],
                  outcomes=[ga4_row(2, qualified=True), ga4_row(1, landing="/other", qualified=True)])
    prior = ingest(session, site, fake.analytics(), scenario="two-pages-observed")
    page = session.scalar(select(m.Page).where(m.Page.url == ORIGIN + "/other"))
    fake.sessions, fake.outcomes = [ga4_row(120)], [ga4_row(3, qualified=True)]
    latest = ingest(session, site, fake.analytics(), scenario="page-disappeared", fetched_at=NOW + timedelta(minutes=1))
    window, provenance = _window(session, site, [page], START, START, [latest, prior])
    assert window.collection_complete is False
    assert window.sessions is None and window.qualified_conversions is None
    assert "missing_page_date_observations" in window.quality_flags
    assert provenance["evidence_ids"] == [latest.id]
    assert session.scalar(select(func.count(m.GA4Daily.id))) == 2  # Historical row retained, not used as fresh.


def test_later_empty_report_is_a_tracking_uncertainty_not_a_zero_or_retained_success(script, db):
    session, site = db
    fake = script(sessions=[ga4_row(100)], outcomes=[ga4_row(2, qualified=True)])
    before = ingest(session, site, fake.analytics(), scenario="observed-before-outage")
    page = session.scalar(select(m.Page))
    fake.sessions, fake.outcomes = [], []
    after = ingest(session, site, fake.analytics(), scenario="possible-tracking-outage",
                   fetched_at=NOW + timedelta(minutes=1))
    window, _ = _window(session, site, [page], START, START, [after, before])
    assert window.collection_complete is False
    assert window.sessions is None and window.qualified_conversions is None
    assert "qualified_outcome_missing" in window.quality_flags
    assert before.content["rows"][0]["qualified_conversions"] == 2


def test_explicit_zero_refresh_replaces_positive_value_without_erasing_its_evidence(script, db):
    session, site = db
    fake = script(sessions=[ga4_row(100)], outcomes=[ga4_row(2, qualified=True)])
    prior = ingest(session, site, fake.analytics(), scenario="positive-before-revision")
    page = session.scalar(select(m.Page))
    fake.sessions, fake.outcomes = [ga4_row(0)], [ga4_row(0, qualified=True)]
    latest = ingest(session, site, fake.analytics(), scenario="explicit-observed-zero",
                    fetched_at=NOW + timedelta(minutes=1))
    window, _ = _window(session, site, [page], START, START, [latest, prior])
    assert window.collection_complete is True
    assert window.sessions == window.qualified_conversions == window.qualified_conversion_value == 0
    assert prior.content["rows"][0]["qualified_conversions"] == 2


@pytest.mark.parametrize("field", ["sessions", "qualified_conversions", "conversion_value"])
def test_mutable_daily_value_cannot_disagree_with_selected_immutable_observation(field, script, db):
    session, site = db
    fake = script(sessions=[ga4_row(100)], outcomes=[ga4_row(2, qualified=True)])
    evidence = ingest(session, site, fake.analytics(), scenario="materialized-value-mismatch")
    page, daily = session.scalar(select(m.Page)), session.scalar(select(m.GA4Daily))
    setattr(daily, field, getattr(daily, field) + 1)
    session.flush()
    window, _ = _window(session, site, [page], START, START, [evidence])
    assert window.collection_complete is False and window.qualified_conversions is None
    assert "api_disagreement" in window.quality_flags


def test_missing_one_page_makes_group_primary_unknown_not_a_partial_sum(script, db):
    session, site = db
    fake = script(sessions=[ga4_row(100), ga4_row(80, landing="/other")],
                  outcomes=[ga4_row(2, qualified=True), ga4_row(1, landing="/other", qualified=True)])
    prior = ingest(session, site, fake.analytics(), scenario="group-before-gap")
    pages = list(session.scalars(select(m.Page)))
    fake.sessions, fake.outcomes = [ga4_row(120)], [ga4_row(3, qualified=True)]
    latest = ingest(session, site, fake.analytics(), scenario="group-after-gap", fetched_at=NOW + timedelta(minutes=1))
    window, _ = _window(session, site, pages, START, START, [latest, prior])
    assert window.collection_complete is False
    assert window.qualified_conversions is None and window.qualified_conversion_value is None
    assert "missing_page_date_observations" in window.quality_flags


def test_fixture_provenance_cannot_be_ingested_into_a_live_site(script, db):
    session, site = db
    batch = fixture_batch(script(sessions=[ga4_row(10)]).analytics(), "fixture-boundary")
    site.config_json = {"source_mode": "live"}
    with pytest.raises(ValueError, match="provenance"):
        ingest_batch(session, site, "ga4", batch)
    assert session.scalar(select(func.count(m.Evidence.id))) == 0
    assert session.scalar(select(func.count(m.GA4Daily.id))) == 0
    assert batch.metadata["qualifies_for_autonomy"] is False


def test_traffic_gain_cannot_override_qualified_outcome_decline(script, db):
    session, site = db
    fake = script(
        sessions=[ga4_row(100 if i < 28 else 200, day=START + timedelta(days=i)) for i in range(56)],
        outcomes=[ga4_row(5 if i < 28 else 3, day=START + timedelta(days=i), qualified=True) for i in range(56)],
    )
    batch = fake.analytics(START, START + timedelta(days=55), page_size=13)
    evidence = ingest(session, site, batch, scenario="goodhart-traffic-up-outcomes-down")
    page = session.scalar(select(m.Page))
    before, _ = _window(session, site, [page], START, START + timedelta(days=27), [evidence])
    after, _ = _window(session, site, [page], START + timedelta(days=28), START + timedelta(days=55), [evidence])
    result = evaluate_experiment(ExperimentSpec(experiment_id="fixture-goodhart-only"), before, after)
    assert result.organic_sessions_change == 1
    assert result.relative_change == pytest.approx(-0.4)
    assert result.verdict == "regression_signal"
    assert "goodhart_traffic_gain_with_primary_outcome_decline" in result.quality_flags
    assert result.causal_effect_identified is False and result.calibration_eligible is False
    assert result.automatic_rollback_authorised is False
    assert session.scalar(select(func.count(m.Action.id))) == 0
    assert site.production_enabled is False and site.autonomy_level == 1
    assert site.config_json["max_daily_actions"] == 0


def test_missing_analytics_and_partial_search_data_remain_explicit(script):
    batch = script(gsc=[gsc_row()]).search(START, START + timedelta(days=1))
    report = data_quality_report(batch.rows, [])
    assert "ga4_unavailable_not_zero" in report["quality_flags"]
    assert report["zero_observations_is_not_zero_business"] is True
    assert batch.metadata["missing_dates"] == [(START + timedelta(days=1)).isoformat()]
    assert "anonymised_queries_omitted_do_not_sum_as_page_totals" in batch.quality_flags
