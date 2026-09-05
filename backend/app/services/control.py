"""Canonical ingestion, deterministic diagnosis and bounded agent coordination.

Providers are selected by trusted site configuration. Failed integrations stay
unknown; there is no live-to-fixture fallback and no production write in a cycle
unless the separately configured executor explicitly permits it.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import math
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from backend.app.config.settings import Settings
from backend.app.contracts import ActionKind, CMSPage, CrawlResult, GA4Row, GSCRow, ProviderUnavailable, VerificationPacket, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.integrations.common import ObservationBatch
from backend.app.services.measurement import evaluate_due_experiments

OBJECTIVE = "Maximise incremental qualified organic conversion value"
PRIVATE_BENCHMARK_FAILURE_PREFIX = "lab_benchmark_"
SAFE_BENCHMARK_ASSESSMENT_FIELDS = frozenset({
    "true_positives", "false_positives", "false_negatives", "precision", "recall",
    "correct_no_action", "false_no_action", "coverage_complete", "high_critical_intercepted",
    "zero_autonomous_production_changes", "structural_benchmark_passed",
})


def serialise(record) -> dict:
    return {column.key: getattr(record, column.key) for column in inspect(record).mapper.column_attrs}


def public_failure(record: m.FailureCase) -> dict:
    """Hide evaluator-private case identity while retaining the learning signal."""
    result = serialise(record)
    if not record.category.startswith(PRIVATE_BENCHMARK_FAILURE_PREFIX):
        return result
    result.update({
        "category": "benchmark_private_scoring_failure",
        "predicted": "Match the independently preregistered benchmark within its engineering thresholds",
        "actual": "At least one private benchmark unit was scored incorrectly; case identity is evaluator-only",
        "root_cause": "A detector precision or coverage gap was reported by the private evaluator",
        "incorrect_assumption": "The detector generalized to every private benchmark unit",
        "missing_evidence": "Case-level evidence is intentionally unavailable to runtime and model consumers",
        "agent_responsible": "deterministic-observer",
        "detection_method": "Signed private benchmark evaluation",
        "preventative_change": "Use aggregate evidence and a new independent holdout; do not tune on private case identity",
        "details_json": {
            "private_case_results_redacted": True,
            "excluded_from_live_calibration": True,
            "level_2_eligible": False,
        },
    })
    return result


def public_action(record: m.Action) -> dict:
    """Remove evaluator-private commitments from general action history."""
    result = serialise(record)
    if record.kind == "evaluate_lab_shadow_benchmark":
        payload = dict(record.payload_json) if isinstance(record.payload_json, dict) else {}
        payload.pop("ground_truth_sha256", None)
        result["payload_json"] = {
            **payload,
            "private_truth_commitment_redacted": True,
        }
    return result


def public_evidence(record: m.Evidence) -> dict:
    """Return ordinary evidence verbatim, but only aggregates for benchmark truth."""
    result = serialise(record)
    if record.source_type != "lab_benchmark":
        return result
    content = record.content if isinstance(record.content, dict) else {}
    assessment = content.get("assessment") if isinstance(content.get("assessment"), dict) else {}
    version = content.get("schema_version")
    if not ((type(version) is int and 0 <= version <= 100) or (
        isinstance(version, str) and re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})?", version)
    )):
        version = None
    aggregate = {
        key: assessment[key]
        for key in SAFE_BENCHMARK_ASSESSMENT_FIELDS
        if key in assessment and (
            assessment[key] is None
            or type(assessment[key]) in {bool, int}
            or (type(assessment[key]) is float and math.isfinite(assessment[key]))
        )
    }
    # Historical labelled rows remain append-only canonical audit records. They
    # are never replayed into agents/API/MCP; only bounded aggregate outcomes are.
    public_source = "lab_benchmark:aggregate-redacted"
    public_content = {
        "schema_version": version,
        "scope": "aggregate_only_private_benchmark",
        "aggregate": aggregate,
        "autonomy_level": 1,
        "production_enabled": False,
        "production_write_budget": 0,
        "paid_api_calls": 0,
        "level_2_eligible": False,
        "private_case_results_redacted": True,
        "private_content_hash_redacted": True,
    }
    # The raw record hash can itself disclose a small, enumerable answer set.
    # General read surfaces receive a hash of this public projection only.
    result.update({
        "source": public_source,
        "content": public_content,
        "content_hash": stable_hash({
            "source": public_source,
            "source_type": "benchmark_aggregate",
            "content": public_content,
        }),
    })
    return result


def site_record(session: Session, site_id: str) -> m.Site:
    site = session.get(m.Site, site_id)
    if not site:
        raise LookupError("Site not found")
    return site


def scoped_record(session: Session, model, site_id: str, record_id: str):
    result = session.scalar(select(model).where(model.id == record_id, model.site_id == site_id))
    if not result:
        raise LookupError("Record not found in this site")
    return result


def local_audit(session: Session, site_id: str, kind: str, actor: str, reason: str, payload: dict,
                event_type: str = "recorded") -> m.Action:
    action = m.Action(site_id=site_id, kind=kind, risk="LOW", actor=actor, reason=reason,
                      idempotency_key=f"{kind}:{uuid4()}", payload_json=payload)
    session.add(action)
    session.flush()
    session.add(m.ActionEvent(site_id=site_id, action_id=action.id, event_type=event_type,
                              details_json={"scope": "canonical_state", "production_write": False}))
    return action


def create_site(session: Session, *, name: str, base_url: str, fixture: bool = False,
                conversion_definition: dict | None = None) -> m.Site:
    from backend.app.integrations.crawler.network import validate_url
    validated = validate_url(base_url, fixture=fixture)
    if fixture and urlsplit(validated).hostname != "example.test":
        raise ValueError("The fixture dataset belongs only to example.test")
    if session.scalar(select(m.Site).where(m.Site.base_url == validated.rstrip("/"))):
        raise ValueError("Site already registered")
    site = m.Site(name=name, base_url=validated.rstrip("/"), autonomy_level=1, production_enabled=False,
        conversion_definition=conversion_definition or {"verified": False, "qualified_events": [], "value_method": None},
        config_json={"source_mode": "fixture" if fixture else "live", "earned_categories": [],
                     "trusted_verifier_ids": ["sceptical-verifier:v1", "human-reviewer"],
                     "trusted_outcome_adjudicators": ["human-reviewer"],
                     "trusted_evidence_owners": ["data-observer", "site-administrator", "human-reviewer"],
                     "max_daily_actions": 5, "automation_suspended": False})
    session.add(site)
    session.flush()
    session.add(m.MissionState(site_id=site.id, objective=OBJECTIVE,
        success_criteria_json=["qualified live conversion definition", "continuous observations", "evidence-backed opportunities",
                              "independent verification", "auditable reversible execution", "measured experiments", "calibrated predictions"],
        constraints_json=["no scaled low-value content", "no fabricated claims", "no destructive operations",
                          "Level 1 default", "human approves production changes", "missing data remains unknown"],
        available_resources_json={"source_mode": site.config_json["source_mode"]},
        unknowns_json=[] if fixture else ["qualified conversion semantics", "provider credentials", "CMS atomic revision support"],
        blockers_json=["Fixture environment: no real business outcomes"] if fixture else ["Live integrations not yet verified"],
        critical_path_json=["ingest", "diagnose", "bounded specialist review", "independent verification", "approve", "execute", "measure"],
        resource_budget_json={"specialists_per_problem": 3, "max_pages_per_crawl": 50, "max_daily_actions": 5},
        autonomy_level=1, phase="shadow", stop_condition="No production changes without verified revision and approval"))
    session.add(m.StrategyVersion(site_id=site.id, version=1, objective=OBJECTIVE, created_by="site-administrator",
        strategy_json={"autonomy": 1, "mode": "shadow", "outcome": "qualified_organic_conversion_value",
                       "proxies": ["sessions", "clicks", "impressions", "CTR", "AI citations"], "production_enabled": False}))
    session.add(m.Assumption(site_id=site.id, statement="The selected conversion definition reflects commercially qualified outcomes",
        owner="governor", impact="All business-value prioritisation depends on this mapping",
        falsification_test="Reconcile configured qualified events with a sample of real CRM bookings or qualified enquiries"))
    local_audit(session, site.id, "register_site", "site-administrator", "Initial Level 1 registration", {"base_url": site.base_url})
    session.commit()
    return site


def ensure_page(session: Session, site: m.Site, url: str) -> m.Page:
    absolute = urljoin(site.base_url + "/", url)
    target, origin = urlsplit(absolute), urlsplit(site.base_url)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.username or target.password:
        raise ValueError("Page observation is outside the registered site origin")
    page = session.scalar(select(m.Page).where(m.Page.site_id == site.id, m.Page.url == absolute))
    if page is None:
        page = m.Page(site_id=site.id, url=absolute)
        session.add(page)
        session.flush()
    return page


def ingest_cms(session: Session, site: m.Site, pages: list[CMSPage], *, is_fixture: bool) -> str:
    if is_fixture != (site.config_json.get("source_mode") == "fixture"):
        raise ValueError("Provider/site provenance mismatch")
    for snapshot in pages:
        page = ensure_page(session, site, snapshot.url)
        previous_hash = page.content_hash
        page.external_id = snapshot.external_id
        page.title, page.content_html, page.meta_description = snapshot.title, snapshot.content, snapshot.meta_description
        page.content_hash = snapshot.fingerprint
        page.last_observed_at = utcnow()
        page.metadata_json = {**page.metadata_json, "cms_snapshot": snapshot.model_dump(mode="json"), "is_fixture": is_fixture}
        if previous_hash != snapshot.fingerprint:
            version = (session.scalar(select(func.max(m.PageVersion.version_number)).where(m.PageVersion.page_id == page.id)) or 0) + 1
            session.add(m.PageVersion(site_id=site.id, page_id=page.id, version_number=version,
                                     content_json=snapshot.model_dump(mode="json"), content_hash=snapshot.fingerprint))
    content = {"page_count": len(pages), "snapshots": [p.model_dump(mode="json") for p in pages]}
    return record_evidence(session, site.id, "cms", "fixture:cms" if is_fixture else site.base_url + "/wp-json/wp/v2", content, is_fixture)


def record_evidence(session: Session, site_id: str, source_type: str, source: str, content: dict, fixture: bool) -> str:
    row = m.Evidence(site_id=site_id, source_type=source_type, source=source, content=content,
                     content_hash=stable_hash(content), is_fixture=fixture, confidence=1.0,
                     owner="data-observer", observed_at=utcnow())
    session.add(row)
    session.flush()
    return row.id


def ingest_batch(session: Session, site: m.Site, kind: str, batch: ObservationBatch) -> str:
    if batch.is_fixture != (site.config_json.get("source_mode") == "fixture"):
        raise ValueError("Provider/site provenance mismatch")
    content = {"rows": [r.model_dump(mode="json") for r in batch.rows], "quality_flags": batch.quality_flags,
               "complete": batch.complete, "metadata": batch.metadata, "fetched_at": batch.fetched_at.isoformat()}
    evidence_id = record_evidence(session, site.id, kind, batch.source, content, batch.is_fixture)
    for row in batch.rows:
        if kind == "gsc":
            # Property-prefix and domain properties can cover more hosts than this registered origin.
            if urlsplit(row.page).netloc != urlsplit(site.base_url).netloc:
                continue
            page = ensure_page(session, site, row.page)
            where = dict(site_id=site.id, date=row.date, page_url=row.page, query=row.query, country=row.country, device=row.device)
            record = session.scalar(select(m.GSCDaily).filter_by(**where))
            if record is None:
                record = m.GSCDaily(**where)
                session.add(record)
            record.page_id, record.clicks, record.impressions, record.position = page.id, row.clicks, row.impressions, row.position
            record.data_state, record.is_fixture = row.data_state, batch.is_fixture
            record.quality_flags_json = batch.quality_flags
            if row.query and not session.scalar(select(m.Query).where(m.Query.site_id == site.id, m.Query.text == row.query)):
                session.add(m.Query(site_id=site.id, text=row.query))
        elif kind == "ga4":
            landing = row.landing_page
            # (not set) isn't an invented URL or a zero-value page.
            page = None if landing.startswith("(") else ensure_page(session, site, landing)
            where = dict(site_id=site.id, date=row.date, landing_page=landing, channel=row.channel)
            record = session.scalar(select(m.GA4Daily).filter_by(**where))
            if record is None:
                record = m.GA4Daily(**where)
                session.add(record)
            record.page_id = page.id if page else None
            for key in ("sessions", "key_events", "qualified_conversions", "conversion_value"):
                setattr(record, key, getattr(row, key))
            record.is_fixture = batch.is_fixture
            record.quality_flags_json = sorted(set(batch.quality_flags + row.quality_flags))
        elif kind == "crawl":
            page = ensure_page(session, site, row.url)
            page.canonical, page.status_code, page.crawlable = row.canonical, row.status_code, row.crawlable
            page.last_observed_at = row.fetched_at
            page.metadata_json = {**page.metadata_json, "crawl_indexability": row.indexability}
            # Eligibility is not confirmed Google indexing; indexed_status is deliberately untouched.
            snap = m.CrawlSnapshot(site_id=site.id, page_id=page.id, url=row.url, status_code=row.status_code,
                result_json=row.model_dump(mode="json"), content_hash=row.content_hash, observed_at=row.fetched_at, is_fixture=batch.is_fixture)
            session.add(snap)
            session.flush()
            for issue in row.issues:
                session.add(m.CrawlIssue(site_id=site.id, page_id=page.id, crawl_snapshot_id=snap.id,
                    kind=str(issue.get("kind", issue.get("type", "crawl_issue"))), severity=str(issue.get("severity", "LOW")).upper(),
                    description=str(issue.get("description", issue.get("message", "Observed technical issue"))), details_json=issue))
        else:
            raise ValueError("Unsupported observation batch")
    session.flush()
    return evidence_id


def cms_for_site(session: Session, site: m.Site, settings: Settings):
    if site.config_json.get("source_mode") == "fixture":
        from backend.app.integrations.fixtures import FixtureCMS
        snapshots = [CMSPage.model_validate(p.metadata_json["cms_snapshot"])
                     for p in session.scalars(select(m.Page).where(m.Page.site_id == site.id))
                     if "cms_snapshot" in p.metadata_json]
        return FixtureCMS(pages=snapshots or None)
    from backend.app.integrations.wordpress.client import WordPressClient
    if not settings.wordpress_url or settings.wordpress_url.rstrip("/") != site.base_url.rstrip("/"):
        raise ProviderUnavailable("WORDPRESS_URL must match the registered site")
    if not settings.wordpress_username or not settings.wordpress_application_password:
        raise ProviderUnavailable("WordPress scoped credentials are not configured")
    return WordPressClient(settings.wordpress_url, settings.wordpress_username,
                           settings.wordpress_application_password.get_secret_value(),
                           meta_description_key=site.config_json.get("wordpress_meta_description_key"))


def latest_crawls(session: Session, site_id: str) -> list[CrawlResult]:
    records = session.scalars(select(m.CrawlSnapshot).where(m.CrawlSnapshot.site_id == site_id).order_by(m.CrawlSnapshot.observed_at.desc(), m.CrawlSnapshot.id.desc()))
    seen, result = set(), []
    for row in records:
        if row.url not in seen:
            result.append(CrawlResult.model_validate(row.result_json))
            seen.add(row.url)
    return result


def observations(session: Session, site: m.Site):
    gsc = [GSCRow(date=r.date, page=r.page_url, query=r.query, country=r.country, device=r.device,
                  clicks=r.clicks, impressions=r.impressions, position=r.position, data_state=r.data_state)
           for r in session.scalars(select(m.GSCDaily).where(m.GSCDaily.site_id == site.id, m.GSCDaily.query != ""))]
    ga4 = [GA4Row(date=r.date, landing_page=r.landing_page, sessions=r.sessions, key_events=r.key_events,
                  qualified_conversions=r.qualified_conversions, conversion_value=r.conversion_value,
                  channel=r.channel, quality_flags=r.quality_flags_json)
           for r in session.scalars(select(m.GA4Daily).where(m.GA4Daily.site_id == site.id))]
    if site.config_json.get("target_kind") == "controlled_test_lab":
        from backend.app.services.test_lab import latest_lab_crawls
        crawls = latest_lab_crawls(session, site.id)
    else:
        crawls = latest_crawls(session, site.id)
    return gsc, ga4, crawls


def analysis_context(session: Session, site: m.Site):
    from backend.app.seo.analysis import AnalysisContext
    fixture = site.config_json.get("source_mode") == "fixture"
    fields = AnalysisContext.model_fields
    data = {"site_url": site.base_url, "inventory_urls": [p.url for p in session.scalars(select(m.Page).where(m.Page.site_id == site.id))],
            "inventory_complete": fixture, "crawl_coverage_complete": fixture,
            "business_values": site.config_json.get("business_values", {}) if site.conversion_definition.get("verified") else {}}
    if site.config_json.get("target_kind") == "controlled_test_lab":
        from backend.app.services.test_lab import analysis_context_data
        data.update(analysis_context_data(session, site))
    return AnalysisContext(**{k: v for k, v in data.items() if k in fields})


def analyze_site(session: Session, site_id: str, *, persist: bool = True) -> dict:
    from backend.app.seo.analysis import analyze, cluster_queries, data_quality_report
    site = site_record(session, site_id)
    gsc, ga4, crawls = observations(session, site)
    context = analysis_context(session, site)
    opportunities = analyze(gsc, ga4, crawls, context)
    evidence_rows = list(session.scalars(select(m.Evidence).where(m.Evidence.site_id == site_id).order_by(m.Evidence.created_at.desc()).limit(10)))
    # The latest collector records form an auditable dependency graph, not independent replications.
    latest = {}
    for evidence in evidence_rows:
        if evidence.source_type in {"gsc", "ga4", "cms", "crawl", "serp", "ai_search", "brand_facts"}:
            dimensions = evidence.content.get("metadata", {}).get("dimensions", [])
            latest.setdefault((evidence.source_type, evidence.source, tuple(dimensions)), evidence.id)
    brand = session.scalar(select(m.Evidence).where(m.Evidence.site_id == site_id,
        m.Evidence.source_type == "brand_facts", m.Evidence.owner == "site-administrator")
        .order_by(m.Evidence.created_at.desc()).limit(1))
    if brand:
        latest[("brand_facts", brand.source, ())] = brand.id
    if persist:
        keys = set()
        for candidate in opportunities:
            key = stable_hash({"site": site_id, "page": candidate.page_url, "kind": candidate.kind, "finding": candidate.finding})
            keys.add(key)
            opportunity = session.scalar(select(m.Opportunity).where(m.Opportunity.site_id == site_id, m.Opportunity.dedup_key == key))
            if opportunity is None:
                opportunity = m.Opportunity(site_id=site_id, dedup_key=key, kind=candidate.kind, finding=candidate.finding)
                session.add(opportunity)
            page = session.scalar(select(m.Page).where(m.Page.site_id == site_id, m.Page.url == candidate.page_url))
            opportunity.page_id = page.id if page else None
            opportunity.page_url, opportunity.score, opportunity.confidence = candidate.page_url, candidate.score, candidate.confidence
            opportunity.components_json, opportunity.evidence_json = candidate.components, candidate.evidence
            opportunity.quality_flags_json = candidate.quality_flags
            opportunity.evidence_ids_json = list(latest.values())
            opportunity.recommended_action, opportunity.status = candidate.recommended_action, "open"
        # Absence in a partial new batch isn't evidence an existing issue was fixed.
        local_audit(session, site_id, "detect_opportunities", "deterministic-observer", "Recomputed evidence-backed diagnostics", {"count": len(opportunities)})
        session.commit()
    return {"opportunities": [x.model_dump(mode="json") for x in opportunities], "query_clusters": cluster_queries([r.query for r in gsc if r.query]),
            "quality": data_quality_report(gsc, ga4, context), "source_mode": site.config_json.get("source_mode"),
            "business_value_verified": bool(site.conversion_definition.get("verified"))}


def ingest_site(session: Session, site_id: str, settings: Settings) -> dict:
    site = site_record(session, site_id)
    status = {}
    if site.config_json.get("target_kind") == "controlled_test_lab":
        from backend.app.services.test_lab import ingest_lab
        status = ingest_lab(session, site, settings)
    elif site.config_json.get("source_mode") == "fixture":
        from backend.app.integrations.fixtures import fixture_crawler, fixture_observations
        cms = cms_for_site(session, site, settings)
        current_pages = cms.list_pages()
        status["cms"] = {"status": "fixture", "evidence_id": ingest_cms(session, site, current_pages, is_fixture=True)}
        batches = fixture_observations()
        batches["crawl"] = fixture_crawler(current_pages).crawl_site(max_pages=min(settings.max_pages_per_crawl, 50))
        for kind, batch in batches.items():
            status[kind] = {"status": "fixture", "evidence_id": ingest_batch(session, site, kind, batch), "rows": len(batch.rows), "quality_flags": batch.quality_flags}
    else:
        from backend.app.integrations.google_search_console.client import GSCClient
        from backend.app.integrations.google_analytics.client import GA4Client
        from backend.app.integrations.crawler.client import Crawler
        end = utcnow().date() - timedelta(days=3)
        start = end - timedelta(days=55)
        # Collector errors are recorded independently; unavailable optional providers do not block the others.
        factories = {
            "cms": lambda: ingest_cms(session, site, cms_for_site(session, site, settings).list_pages(), is_fixture=False),
            "gsc": lambda: ingest_batch(session, site, "gsc", GSCClient(settings.gsc_property).fetch(start, end)),
            "gsc_page_totals": lambda: ingest_batch(session, site, "gsc", GSCClient(settings.gsc_property).fetch_page_totals(start, end)),
            "ga4": lambda: ingest_batch(session, site, "ga4", GA4Client(settings.ga4_property_id).fetch(
                start, end, conversion_definition=site.conversion_definition if site.conversion_definition.get("verified") is True else None)),
            "crawl": lambda: ingest_batch(session, site, "crawl", Crawler(site.base_url).crawl_site(max_pages=min(settings.max_crawl_pages, 50))),
        }
        for name, operation in factories.items():
            try:
                with session.begin_nested():
                    evidence_id = operation()
                status[name] = {"status": "observed", "evidence_id": evidence_id}
            except Exception as error:
                status[name] = {"status": "unavailable", "error_type": type(error).__name__, "meaning": "unknown, not zero"}
    mission = session.scalar(select(m.MissionState).where(m.MissionState.site_id == site_id))
    if mission:
        mission.available_resources_json = {"sources": status, "source_mode": site.config_json.get("source_mode")}
        mission.blockers_json = [f"{name} unavailable" for name, data in status.items() if data["status"] == "unavailable"]
        if not site.conversion_definition.get("verified"):
            mission.blockers_json = mission.blockers_json + ["Qualified conversion definition unverified"]
        if site.config_json.get("source_mode") == "fixture":
            mission.blockers_json = mission.blockers_json + ["Fixture observations cannot establish real SEO impact"]
        mission.updated_at = utcnow()
    local_audit(session, site_id, "ingest_observations", "data-observer", "Scheduled canonical observation collection", status)
    session.commit()
    return status


def agent_evidence(session: Session, site_id: str, evidence_ids: list[str]) -> list[dict]:
    result = []
    for evidence_id in evidence_ids[:12]:
        evidence = scoped_record(session, m.Evidence, site_id, evidence_id)
        public = public_evidence(evidence)
        content = public["content"]
        if evidence.source_type == "lab_benchmark":
            result.append({
                "id": evidence.id,
                "source": public["source"],
                "source_type": "benchmark_aggregate",
                "source_trust": "trusted_measurement",
                "observed_at": evidence.observed_at.isoformat(),
                "content": content,
            })
            continue
        # Keep collector provenance and an explicit bounded sample; raw full records remain in canonical DB.
        sampled = {k: v for k, v in content.items() if k not in {"rows", "snapshots"}}
        rows = content.get("rows", content.get("snapshots", []))
        sampled.update({"sample": rows[:12], "total_records": len(rows), "sample_is_complete": len(rows) <= 12})
        result.append({"id": evidence.id, "source": public["source"], "source_type": evidence.source_type,
                       "source_trust": "fixture" if evidence.is_fixture else (
                           "trusted_operator" if evidence.source_type == "brand_facts" and evidence.owner == "site-administrator" else
                           "untrusted_external" if evidence.source_type in {"crawl", "serp", "ai_search"} else "trusted_measurement"),
                       "observed_at": evidence.observed_at.isoformat(), "content": sampled})
    return result


def prior_failures(session: Session, site_id: str) -> list[dict]:
    # JSON-safe bounded histories retain negative evidence without exposing secrets.
    return [{"id": f.id, "category": f.category, "predicted": f.predicted, "actual": f.actual,
             "root_cause": f.root_cause, "incorrect_assumption": f.incorrect_assumption,
             "preventative_change": f.preventative_change, "created_at": f.created_at.isoformat()}
            # The benchmark evaluator sees private labels after diagnosis. Its
            # failure records remain auditable but cannot contaminate the next
            # blinded specialist invocation with the held-out answer key.
            for f in session.scalars(select(m.FailureCase).where(m.FailureCase.site_id == site_id,
                                      ~m.FailureCase.category.startswith("lab_benchmark_"))
                                      .order_by(m.FailureCase.created_at.desc()).limit(10))]


def run_specialists(session: Session, site: m.Site, settings: Settings) -> dict:
    from backend.app.agents.runtime import analyze_problem
    from backend.app.services.agent_audit import runtime_options
    opportunity_query = select(m.Opportunity).where(m.Opportunity.site_id == site.id, m.Opportunity.status == "open").order_by(m.Opportunity.score.desc())
    if site.config_json.get("target_kind") == "controlled_test_lab":
        from backend.app.services.test_lab import latest_lab_evidence
        current_crawl = latest_lab_evidence(session, site.id)
        opportunity = next((item for item in session.scalars(opportunity_query)
                            if current_crawl and current_crawl.id in item.evidence_ids_json), None)
    else:
        opportunity = session.scalar(opportunity_query)
    if not opportunity:
        return {"decision": "NO-ACTION", "reason": "No supported opportunity"}
    task = m.Task(site_id=site.id, title=f"Investigate {opportunity.kind}", objective=opportunity.finding,
                  opportunity_id=opportunity.id, status="running", priority=opportunity.score)
    session.add(task)
    session.flush()
    failures = prior_failures(session, site.id)
    problem = {"business_objective": OBJECTIVE, "page_url": opportunity.page_url, "kind": opportunity.kind,
               "symptoms": {"finding": opportunity.finding, "quality_flags": opportunity.quality_flags_json,
                            "detector_evidence": opportunity.evidence_json},
               "business_conversion_definition": site.conversion_definition, "autonomy_level": site.autonomy_level,
               "production_enabled": False}
    options = runtime_options(session, site, settings, task_id=task.id)
    mode = options["mode"]
    evidence = agent_evidence(session, site.id, opportunity.evidence_ids_json)
    result = asyncio.run(analyze_problem(problem, evidence, prior_failures=failures, **options))
    task.status, task.result_json = "completed", result
    task.contract_json = {"objective": task.objective, "scope": [opportunity.page_url], "non_goals": ["production mutation", "new unsupported business facts"]}
    runs = {row.agent_name: row for row in session.scalars(select(m.AgentRun).where(m.AgentRun.task_id == task.id))}
    for item in result.get("findings", []):
        packet = item.get("packet", {})
        if not isinstance(packet, dict) or not packet.get("finding"):
            continue
        claim = m.Claim(site_id=site.id, claim=packet["finding"], claim_type="INFERENCE", source="bounded-specialist",
            evidence_ids_json=packet.get("supporting_evidence", []), confidence=packet.get("confidence", 0),
            owner=item.get("role", "specialist"), contradicting_evidence_json=packet.get("contradicting_evidence", []),
            alternative_explanations_json=packet.get("alternative_explanations", []))
        session.add(claim)
        session.flush()
        run = runs.get(item.get("role"))
        if run:
            session.add(m.AgentFinding(site_id=site.id, agent_run_id=run.id, claim_id=claim.id, packet_json=packet))
    decision = result.get("decision", "NO-ACTION")
    if decision == ActionKind.UPDATE_TITLE.value and opportunity.page_id and mode == "live":
        if site.config_json.get("target_kind") == "controlled_test_lab":
            result["revision_proposal"] = {"status": "NEEDS_EVIDENCE", "reason": "Static test lab has no CMS mutation capability; prepare an independently reviewed Git revision"}
        else:
            result["revision_proposal"] = propose_model_title(session, site, opportunity, problem, evidence, failures, settings, task.id)
        task.result_json = result
    session.add(m.DecisionLog(site_id=site.id, decision=str(decision), rationale="Bounded specialists and independent evidence-first verification",
        owner="mission-governor", evidence_ids_json=opportunity.evidence_ids_json,
        uncertainty_json=opportunity.quality_flags_json + (["Fixture reasoning: no live model call"] if mode == "fixture" else []),
        alternatives_json=["NO-ACTION", "gather additional evidence", "prepare revision for human review"],
        regret_json={"wrong_action_cost": "potential conversion or integrity damage", "delayed_action_cost": "unverified opportunity value"}))
    session.commit()
    return {"task_id": task.id, **result}


def propose_model_title(session, site, opportunity, problem, evidence, failures, settings, task_id):
    """Only a supported, extractive title can become an automatic concrete draft."""
    from backend.app.agents.runtime import draft_metadata, verify_proposal
    from backend.app.services.agent_audit import runtime_options
    from backend.app.services.execution import propose_revision, record_verification
    page = scoped_record(session, m.Page, site.id, opportunity.page_id)
    before = CMSPage.model_validate(page.metadata_json["cms_snapshot"])
    options = runtime_options(session, site, settings, task_id=task_id)
    draft = asyncio.run(draft_metadata({**problem, "before": before.model_dump(mode="json")}, evidence,
                                      prior_failures=failures, **options))
    if not draft.get("proposal") or draft.get("before_fingerprint") != before.fingerprint:
        return {"status": "NEEDS_EVIDENCE", "draft": draft}
    title = draft["proposal"]
    after = CMSPage.model_validate(before.model_dump() | {"title": title["title"]})
    latest = session.scalar(select(func.max(m.GA4Daily.date)).where(m.GA4Daily.site_id == site.id,
                                                                   m.GA4Daily.page_id == page.id))
    experiment = m.Experiment(site_id=site.id, page_id=page.id, hypothesis=title["reason"],
        mechanism="An evidence-supported title may improve qualified visitor matching",
        primary_outcome="qualified_organic_conversion_value", baseline_start=latest - timedelta(days=27) if latest else None,
        baseline_end=latest, evaluation_windows_json=[7, 14, 28, 56],
        alternative_explanations_json=["demand", "seasonality", "tracking", "SERP changes"])
    # Epistemic title confidence is not a forecast probability of conversion gain.
    session.add(experiment)
    session.flush()
    revision_result = propose_revision(session, site_id=site.id, page_id=page.id, kind=ActionKind.UPDATE_TITLE,
        after=after, created_by="content-strategist", reason=title["reason"], evidence_ids=title["evidence_ids"], experiment_id=experiment.id)
    if not revision_result.get("revision_id"):
        return revision_result
    revision = session.get(m.Revision, revision_result["revision_id"])
    proposal = {"finding": title["reason"], "confidence": title["confidence"], "supporting_evidence": title["evidence_ids"],
                "recommended_action": "update_title", "expected_impact": "Unverified qualified-conversion effect",
                "risk": "MEDIUM", "reversibility": 1, "uncertainty": title["uncertainty"], "needs_human_review": True}
    verified = asyncio.run(verify_proposal(problem, proposal, evidence, proposer_id="content-strategist",
        revision_target={"before": revision.before_json, "after": revision.after_json, "revision_hash": revision.revision_hash},
        prior_failures=failures, **runtime_options(session, site, settings, task_id=task_id)))
    if settings.service_role == "worker":
        # The scheduler's database identity cannot issue authoritative
        # Verification rows. Retain the bounded adversarial analysis as a
        # preview; an API/reviewer path must independently verify the revision.
        return {
            **revision_result,
            "status": "awaiting_api_verification",
            "verification_preview": verified["verification"],
            "authoritative_verification_recorded": False,
        }
    record_verification(session, revision_id=revision.id, packet=VerificationPacket.model_validate(verified["verification"]))
    return {
        **revision_result,
        "status": "awaiting_human_approval",
        "verification": verified["verification"],
        "authoritative_verification_recorded": True,
    }


def execute_eligible_revisions(session: Session, site: m.Site, settings: Settings) -> dict:
    from backend.app.services.execution import execute_revision
    if site.config_json.get("source_mode") == "fixture" or settings.shadow_mode or not settings.production_enabled or not site.production_enabled:
        return {"status": "shadow", "executed": [], "reason": "Production writes are disabled"}
    limit = min(settings.max_daily_actions, int(site.config_json.get("max_daily_actions", 5)))
    if limit <= 0 or site.config_json.get("automation_suspended"):
        return {"status": "budget_or_policy_blocked", "executed": []}
    revisions = list(session.scalars(select(m.Revision).where(m.Revision.site_id == site.id)
                                    .order_by(m.Revision.created_at.desc()).limit(500)))
    selected = []
    for revision in revisions:
        executed = session.scalar(select(m.ActionEvent.id).join(m.Action, m.Action.id == m.ActionEvent.action_id).where(
            m.Action.revision_id == revision.id, m.ActionEvent.event_type == "succeeded").limit(1))
        if executed:
            continue
        approval = session.scalar(select(m.Approval).where(m.Approval.revision_id == revision.id)
                                  .order_by(m.Approval.created_at.desc(), m.Approval.id.desc()))
        human_approved = bool(approval and approval.decision == "APPROVE" and approval.expires_at and approval.expires_at > utcnow())
        verification = session.scalar(select(m.Verification).where(m.Verification.revision_id == revision.id)
                                      .order_by(m.Verification.created_at.desc()).limit(1))
        earned = min(site.autonomy_level, settings.autonomy_level) >= 2 and revision.kind in site.config_json.get("earned_categories", [])
        if (human_approved or earned) and verification and verification.verdict == "PASS":
            selected.append((revision, approval.id if approval else "earned", verification.id))
        if len(selected) >= limit:
            break
    cms = cms_for_site(session, site, settings) if selected else None
    results = [execute_revision(session, cms, revision_id=r.id, actor="bounded-executor",
               idempotency_key=f"cycle:{r.id}:{approval_id}:{verification_id}", production_enabled=True,
               max_daily_actions=settings.max_daily_actions, max_autonomy_level=settings.autonomy_level)
               for r, approval_id, verification_id in selected]
    return {"status": "guarded_queue_evaluated", "results": results}


def run_cycle(session: Session, site_id: str, settings: Settings, *, idempotency_key: str | None = None) -> dict:
    from backend.app.scheduler.locking import fenced_site_work
    site_record(session, site_id)
    with fenced_site_work(session, site_id, owner=f"mission-governor:{uuid4()}", ttl_seconds=settings.scheduler_lease_seconds) as lease:
        if lease is None:
            return {"status": "busy", "reason": "Another worker owns this site's observation cycle"}
        return _run_leased_cycle(session, site_id, settings, idempotency_key=idempotency_key)


def _run_leased_cycle(session: Session, site_id: str, settings: Settings, *, idempotency_key: str | None = None) -> dict:
    site = site_record(session, site_id)
    key = idempotency_key or str(uuid4())
    # The caller can retain an older ORM identity across lease acquisition.
    # Recovery decisions must use the completed state committed by any prior
    # worker, not overwrite it from an expire_on_commit=False session cache.
    previous = session.scalar(select(m.JobRun).where(m.JobRun.site_id == site_id,
        m.JobRun.job_name == "seo-control-loop", m.JobRun.idempotency_key == key)
        .execution_options(populate_existing=True))
    if previous:
        if previous.status == "running":
            # We hold a new site lease, so this is an abandoned durable run,
            # not an active concurrent worker. A paid-capable/executor-capable
            # cycle cannot be safely replayed from an unknown stage. Make the
            # interruption explicit once, preserving partial evidence/results.
            previous.status, previous.completed_at, previous.error = "reconciliation_required", utcnow(), "InterruptedCycle"
            previous.result_json = {**previous.result_json, "recovery": {
                "state": "unknown", "retry_safe": False, "requires_human_review": True}}
            audit = local_audit(session, site_id, "control_loop_recovery", "mission-governor",
                "Interrupted cycle requires reconciliation; no automatic re-execution",
                {"job_id": previous.id, "production_write": False}, event_type="reconciliation_required")
            session.add(m.FailureCase(site_id=site_id, action_id=audit.id, category="control_loop_interrupted",
                predicted="Complete a bounded observation cycle", actual="Prior cycle has no durable terminal outcome",
                root_cause="Worker termination, lease loss or unavailable storage; exact cause unknown",
                agent_responsible="mission-governor", detection_method="Lease-fenced restart reconciliation",
                preventative_change="Reconcile committed stages and reservations before authorising any retry",
                details_json={"job_id": previous.id, "state": "unknown", "retry_safe": False}))
            session.commit()
        return {"job_id": previous.id, "status": previous.status, "idempotent_replay": True, "result": previous.result_json}
    job = m.JobRun(site_id=site_id, job_name="seo-control-loop", idempotency_key=key, owner="mission-governor")
    session.add(job)
    session.commit()
    result = {}
    try:
        result["ingestion"] = ingest_site(session, site_id, settings)
        result["diagnosis"] = analyze_site(session, site_id)
        result["specialists"] = run_specialists(session, site, settings) if site.autonomy_level >= 1 else {"decision": "NO-ACTION", "reason": "Observer mode"}
        result["execution"] = execute_eligible_revisions(session, site, settings)
        if settings.service_role == "worker":
            result["measurement"] = evaluate_due_experiments(
                session, site_id, authority_updates_allowed=False,
            )
        else:
            result["measurement"] = evaluate_due_experiments(session, site_id)
        job.status, job.completed_at, job.result_json = "completed", utcnow(), result
        session.commit()
        return {"job_id": job.id, "status": job.status, "result": result}
    except Exception as error:
        session.rollback()
        job = session.get(m.JobRun, job.id)
        job.status, job.completed_at, job.error = "failed", utcnow(), type(error).__name__
        job.result_json = result
        session.add(m.FailureCase(site_id=site_id, category="control_loop", predicted="Complete a bounded observation cycle",
            actual=f"Cycle stopped: {type(error).__name__}", root_cause="Requires diagnostic review", agent_responsible="mission-governor",
            detection_method="controlled exception boundary", preventative_change="Resolve collector/runtime issue before re-running", details_json={"job_id": job.id}))
        session.commit()
        raise
