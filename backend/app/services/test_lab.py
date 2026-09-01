"""A bounded, write-disabled target and a runtime-isolated structural benchmark.

Only the evaluator reads the ground-truth labels, after the observation and
decision packet is committed. Owner-attested release inventories describe files
and page purposes, never expected defects. Artifact transport is explicitly
fixture data; public transport cannot fall back to local files or invented data.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config.settings import Settings
from backend.app.contracts import ActionKind, CrawlResult, StrictModel, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.guardrails.policy import LOCAL_KINDS, classify_risk, evaluate_policy
from backend.app.integrations.common import ObservationBatch, ProviderError
from backend.app.integrations.crawler.client import Crawler
from backend.app.integrations.crawler.network import validate_url

TARGET_KIND = "controlled_test_lab"
FIXTURE_ORIGIN = "https://example.test"
MAX_PAGES = 50
MAX_MANIFEST_BYTES = 128_000
REMEDIATION = {
    "orphan_page": ActionKind.ADD_INTERNAL_LINK,
    "potential_orphan_page": ActionKind.ADD_INTERNAL_LINK,
    "weak_internal_links": ActionKind.ADD_INTERNAL_LINK,
    "broken_internal_link": ActionKind.ADD_INTERNAL_LINK,
    "duplicate_title": ActionKind.UPDATE_TITLE,
    "duplicate_meta_description": ActionKind.UPDATE_META_DESCRIPTION,
    "thin_content": ActionKind.UPDATE_EXISTING_COPY,
    "potential_topic_overlap": ActionKind.UPDATE_EXISTING_COPY,
    "canonical_mismatch": ActionKind.CHANGE_CANONICAL,
    "accidental_noindex": ActionKind.CHANGE_ROBOTS,
    "sitemap_missing_page": ActionKind.DEPLOY_CODE,
    "sitemap_unknown_page": ActionKind.DEPLOY_CODE,
}


def _path(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 500 or not value.startswith("/")
            or value.startswith("//") or "\\" in value or "%" in value
            or any(ord(char) < 33 for char in value)
            or any(part in {".", ".."} for part in value.split("/"))):
        raise ValueError("A lab path must be a bounded, unambiguous root-relative path")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or parsed.netloc:
        raise ValueError("A lab path cannot contain an origin, query or fragment")
    return value


class InventoryPage(StrictModel):
    path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_indexing: Literal["index"]
    purpose: Literal["guide", "hub", "utility", "exercise", "reference", "home", "note"]

    _validate_path = field_validator("path")(_path)


class InventoryManifest(StrictModel):
    schema_version: Literal[1]
    base_url: str
    pages: list[InventoryPage] = Field(min_length=1, max_length=MAX_PAGES)

    @model_validator(mode="after")
    def unique_pages(self):
        paths = [page.path for page in self.pages]
        if len(paths) != len(set(paths)) or "/" not in paths:
            raise ValueError("Inventory must contain one home page and unique paths")
        return self


def validate_manifest(payload: bytes, *, expected_sha256: str, base_url: str) -> InventoryManifest:
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("An exact owner-attested inventory SHA-256 is required")
    if len(payload) > MAX_MANIFEST_BYTES or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Inventory bytes differ from the owner-attested release")
    manifest = InventoryManifest.model_validate_json(payload)
    if manifest.base_url.rstrip("/") != base_url.rstrip("/"):
        raise ValueError("Inventory origin differs from the registered target")
    return manifest


def artifact_client(build_dir: Path) -> httpx.Client:
    """Model only generated static bytes on the reserved fixture origin."""
    root = build_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Artifact build directory must exist")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method not in {"GET", "HEAD"} or str(request.url).split("/", 3)[:3] != ["https:", "", "example.test"]:
            raise ValueError("Artifact transport only supports reserved-origin reads")
        path = _path(request.url.path)
        target = root / path.lstrip("/")
        if path.endswith("/"):
            target = target / "index.html"
        elif target.is_dir():
            return httpx.Response(308, headers={"location": path + "/"})
        target = target.resolve()
        if not target.is_relative_to(root):
            raise ValueError("Artifact route escaped its release directory")
        status = 200
        if not target.is_file():
            status, target = 404, root / "404.html"
        content = target.read_bytes() if target.is_file() else b"<!doctype html><title>Page not found | SEO Test Lab</title>"
        if len(content) > 2_000_000:
            raise ValueError("Artifact response exceeds crawler budget")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return httpx.Response(status, content=content if request.method == "GET" else b"",
                              headers={"content-type": content_type, "x-spiral-provenance": "fixture"})

    return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False, trust_env=False)


def register_lab(session: Session, *, mode: Literal["artifact", "public"], base_url: str,
                 expected_manifest_sha256: str, build_dir: Path | None = None,
                 gsc_property: str | None = None, ga4_property_id: str | None = None) -> m.Site:
    from backend.app.services import control
    if mode not in {"artifact", "public"}:
        raise ValueError("Select artifact or public mode explicitly")
    fixture = mode == "artifact"
    origin = validate_url(base_url, fixture=fixture).rstrip("/")
    parsed = urlsplit(origin)
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("The lab target must be an origin")
    if fixture and origin != FIXTURE_ORIGIN:
        raise ValueError("Artifact mode is restricted to https://example.test")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise ValueError("An owner-attested inventory SHA-256 is required")
    if fixture:
        if build_dir is None:
            raise ValueError("Artifact mode requires the generated build directory")
        validate_manifest((build_dir / "inventory.json").read_bytes(), expected_sha256=expected_manifest_sha256,
                          base_url=origin)
    site = session.scalar(select(m.Site).where(m.Site.base_url == origin))
    if site and (site.config_json.get("target_kind") != TARGET_KIND
                 or site.config_json.get("source_mode") != ("fixture" if fixture else "live")):
        raise ValueError("Refusing to repurpose an existing site; use a separate lab database")
    if site and (site.autonomy_level != 1 or site.production_enabled or site.config_json.get("earned_categories")):
        raise ValueError("Lab benchmark requires an existing Level 1 site with production writes disabled")
    if site and site.conversion_definition.get("verified"):
        raise ValueError("A lab cannot overwrite a verified commercial conversion definition")
    test_conversion = {
        "verified": False, "qualified_events": [], "value_method": None,
        "purpose": "test_only", "commercial_conversion_value": None,
        "test_events": ["lab_checklist_complete"],
        "limitation": "Demonstration events are not commercially qualified conversions or revenue",
    }
    if site is None:
        site = control.create_site(session, name="Spiral Max SEO Test Lab" + (" — artifact fixture" if fixture else " — public demonstration"),
            base_url=origin, fixture=fixture, conversion_definition=test_conversion)
    config = {"mode": mode, "expected_manifest_sha256": expected_manifest_sha256,
              "inventory_path": "/inventory.json", "provider_bindings": {
                  "gsc_property": gsc_property, "ga4_property_id": ga4_property_id}}
    if fixture:
        config["artifact_build_dir"] = str(build_dir.resolve())
    previous = site.config_json.get("test_lab")
    if (previous != config or site.config_json.get("target_kind") != TARGET_KIND
            or site.conversion_definition != test_conversion):
        before_conversion_hash = stable_hash(site.conversion_definition)
        site.conversion_definition = test_conversion
        site.config_json = {**site.config_json, "target_kind": TARGET_KIND, "test_lab": config,
                            "earned_categories": [], "max_daily_actions": 0}
        mission = session.scalar(select(m.MissionState).where(m.MissionState.site_id == site.id))
        mission.phase = "controlled_test_lab_shadow"
        mission.unknowns_json = ["Real search indexing", "GA4 test-event delivery", "Live model quality", "Commercial conversion value is not a lab objective"]
        mission.constraints_json = mission.constraints_json + ["Controlled demo site only", "No autonomous production writes", "Structural benchmark cannot earn Level 2"]
        mission.critical_path_json = ["attest release inventory", "crawl", "diagnose", "bounded independent review",
                                      "freeze observations", "evaluate seeded ground truth", "verify public/browser/rollback gates"]
        mission.updated_at = utcnow()
        control.local_audit(session, site.id, "configure_test_lab", "site-administrator",
            "Register an owner-attested, write-disabled controlled demonstration", {
                "mode": mode, "manifest_sha256": expected_manifest_sha256,
                "production_enabled": False, "autonomy_level": 1,
                "before_conversion_hash": before_conversion_hash, "after_conversion_hash": stable_hash(test_conversion),
                "commercial_value_verified": False, "provider_bindings": config["provider_bindings"]})
        session.commit()
    return site


def collect_lab(site: m.Site, *, max_pages: int = MAX_PAGES) -> tuple[ObservationBatch, dict]:
    """Collect verified release bytes without consulting benchmark labels."""
    config = site.config_json["test_lab"]
    fixture = config["mode"] == "artifact"
    client = artifact_client(Path(config["artifact_build_dir"])) if fixture else None
    crawler = Crawler(site.base_url, client=client, fixture_mode=fixture, min_interval=0 if fixture else 1)
    try:
        manifest_url = site.base_url + "/inventory.json"
        status, _, payload, final, _ = crawler._fetch(manifest_url, enforce_robots=True)
        if status != 200 or final != manifest_url:
            raise ProviderError("Owner-attested inventory was not served at its fixed URL")
        manifest = validate_manifest(payload, expected_sha256=config["expected_manifest_sha256"], base_url=site.base_url)
        inventory = [urljoin(site.base_url + "/", page.path) for page in manifest.pages]
        batch = crawler.crawl_site(max_pages=min(max_pages, MAX_PAGES), inventory_urls=inventory)
        rows = {row.url: row for row in batch.rows}
        mismatches = []
        for page, url in zip(manifest.pages, inventory, strict=True):
            row = rows.get(url)
            if row is None or row.status_code != 200 or row.final_url != url or row.content_hash != page.content_sha256:
                mismatches.append(url)
        # An unlisted, successful HTML page invalidates an owner assertion that
        # this release contains the full navigable inventory.
        unexpected = [row.url for row in batch.rows if row.status_code == 200 and row.url not in inventory]
        unobserved = [row.url for row in batch.rows if row.status_code not in {200, 404, 410}]
        incomplete = {"page_budget_reached", "link_budget_reached", "queue_budget_reached", "crawl_deadline_reached"}
        bad_issues = {"fetch_blocked", "robots_unknown", "robots_blocked", "unsupported_content_type", "link_budget_reached"}
        verified = not mismatches and not unexpected
        graph_complete = (verified and not unobserved and batch.metadata.get("queue_exhausted") is True
                          and not incomplete.intersection(batch.quality_flags)
                          and not any(issue.get("kind") in bad_issues for row in batch.rows for issue in row.issues))
        attestation = {
            "manifest_sha256": config["expected_manifest_sha256"], "manifest_origin": manifest.base_url,
            "manifest_verified": True, "inventory_urls": inventory, "inventory_complete": verified,
            "crawl_coverage_complete": graph_complete, "mismatched_pages": mismatches,
            "unexpected_pages": unexpected, "unobserved_pages": unobserved,
            "page_purposes": {url: page.purpose for url, page in zip(inventory, manifest.pages, strict=True)} if verified else {},
            "intended_indexable_urls": inventory if verified else [],
            "sitemap_urls": batch.metadata.get("sitemap_urls", []),
            "sitemap_complete": batch.metadata.get("sitemap_complete", False),
            "trust_basis": "Exact owner-attested release inventory bytes and each listed response body hash",
            "observed_at": batch.fetched_at.isoformat(), "is_fixture": fixture,
        }
        batch.source = "fixture:test_lab:crawler" if fixture else "test_lab:crawler"
        batch.metadata = {**batch.metadata, "test_lab_attestation": attestation}
        batch.complete = graph_complete
        batch.quality_flags = sorted(set(batch.quality_flags + ["controlled_test_lab", "commercial_value_unverified"]
            + ([] if verified else ["release_inventory_verification_failed"])
            + ([] if graph_complete else ["lab_graph_incomplete"])))
        return batch, attestation
    finally:
        crawler.client.close()


def ingest_lab(session: Session, site: m.Site, settings: Settings) -> dict:
    from backend.app.services import control
    if site.production_enabled or site.autonomy_level != 1:
        raise ValueError("Lab ingestion requires Level 1 and production writes disabled")
    fixture = site.config_json["test_lab"]["mode"] == "artifact"
    status = {"cms": {"status": "not_applicable", "meaning": "Static release; no CMS mutation capability"}}
    try:
        batch, attestation = collect_lab(site, max_pages=min(settings.max_crawl_pages, settings.max_pages_per_crawl))
        crawl_status = "fixture" if fixture else "observed"
    except Exception as error:
        # Persist a new empty failed batch. Old pages must not supply missing
        # coverage or masquerade as observations from the current deployment.
        attestation = {"manifest_verified": False, "inventory_complete": False, "crawl_coverage_complete": False,
                       "inventory_urls": [], "sitemap_urls": [], "sitemap_complete": False,
                       "page_purposes": {}, "intended_indexable_urls": [], "is_fixture": fixture,
                       "error_type": type(error).__name__, "meaning": "Unknown; no fixture fallback"}
        batch = ObservationBatch([], "fixture:test_lab:crawler" if fixture else "test_lab:crawler",
            ["lab_collection_failed", "lab_graph_incomplete"] + (["fixture_data"] if fixture else []),
            complete=False, metadata={"test_lab_attestation": attestation})
        crawl_status = "unavailable"
        session.add(m.FailureCase(site_id=site.id, category="test_lab_collection", predicted="Collect owner-attested lab release",
            actual=f"Collection unavailable: {type(error).__name__}", root_cause="Inventory, network or crawl verification failed",
            agent_responsible="data-observer", detection_method="Fail-closed release attestation",
            preventative_change="Inspect deployment and owner-attested manifest before re-running",
            details_json={"is_fixture": fixture}))
    evidence_id = control.ingest_batch(session, site, "crawl", batch)
    status["crawl"] = {"status": crawl_status, "evidence_id": evidence_id, "rows": len(batch.rows),
                       "quality_flags": batch.quality_flags, "coverage": attestation}
    # The static inventory is a read-only source. No synthetic CMS snapshot is
    # invented, which keeps the existing CMS executor unavailable on this site.
    for row in batch.rows:
        page = control.ensure_page(session, site, row.url)
        page.title, page.meta_description, page.content_hash = row.title, row.meta_description, row.content_hash
        page.metadata_json = {**page.metadata_json, "target_kind": TARGET_KIND, "is_fixture": fixture,
                              "last_crawl_evidence_id": evidence_id}
    bindings = site.config_json["test_lab"].get("provider_bindings", {})
    end = utcnow().date() - timedelta(days=3)
    start = end - timedelta(days=55)
    from backend.app.integrations.google_search_console.client import GSCClient
    from backend.app.integrations.google_analytics.client import GA4Client
    for source in ("gsc", "ga4"):
        configured = settings.gsc_property if source == "gsc" else settings.ga4_property_id
        bound = bindings.get("gsc_property" if source == "gsc" else "ga4_property_id")
        if fixture or not configured or configured != bound:
            status[source] = {"status": "unavailable", "meaning": "Unknown; no synthetic analytics",
                              "reason": "artifact_mode" if fixture else "site_specific_provider_binding_required"}
            continue
        try:
            with session.begin_nested():
                if source == "gsc":
                    if not _gsc_scope_matches(configured, site.base_url):
                        raise ValueError("Search Console property differs from target")
                    data = GSCClient(configured).fetch(start, end)
                else:
                    # Demo events never become verified financial outcomes.
                    data = GA4Client(configured).fetch(start, end, conversion_definition=None)
                observed_id = control.ingest_batch(session, site, source, data)
            status[source] = {"status": "observed", "evidence_id": observed_id}
        except Exception as error:
            status[source] = {"status": "unavailable", "error_type": type(error).__name__, "meaning": "Unknown, not zero"}
    return status


def _gsc_scope_matches(property_id: str, base_url: str) -> bool:
    if property_id.startswith("sc-domain:"):
        return property_id.removeprefix("sc-domain:") == urlsplit(base_url).hostname
    return property_id.rstrip("/") == base_url.rstrip("/")


def latest_lab_evidence(session: Session, site_id: str) -> m.Evidence | None:
    return session.scalar(select(m.Evidence).where(m.Evidence.site_id == site_id, m.Evidence.source_type == "crawl",
        m.Evidence.source.in_(["fixture:test_lab:crawler", "test_lab:crawler"]))
        .order_by(m.Evidence.created_at.desc(), m.Evidence.id.desc()).limit(1))


def latest_lab_crawls(session: Session, site_id: str) -> list[CrawlResult]:
    evidence = latest_lab_evidence(session, site_id)
    return [CrawlResult.model_validate(row) for row in evidence.content.get("rows", [])] if evidence else []


def analysis_context_data(session: Session, site: m.Site) -> dict:
    evidence = latest_lab_evidence(session, site.id)
    return _context_from_evidence(evidence)


def _context_from_evidence(evidence: m.Evidence | None) -> dict:
    context = evidence.content.get("metadata", {}).get("test_lab_attestation", {}) if evidence else {}
    # Only this attestation, produced by the fixed collector, can assert coverage.
    allowed = {"inventory_urls", "inventory_complete", "crawl_coverage_complete", "sitemap_urls",
               "sitemap_complete", "page_purposes", "intended_indexable_urls"}
    defaults = {"inventory_urls": [], "inventory_complete": False, "crawl_coverage_complete": False}
    return {**defaults, **{key: value for key, value in context.items() if key in allowed}}


def _related_urls(candidate: dict) -> set[str]:
    result = {candidate["page_url"]}
    for item in candidate.get("evidence", []):
        for field in ("pages", "incoming_sources"):
            for value in item.get(field, []):
                if isinstance(value, str):
                    result.add(value)
                elif isinstance(value, dict) and isinstance(value.get("url"), str):
                    result.add(value["url"])
        if isinstance(item.get("canonical_target"), str):
            result.add(item["canonical_target"])
    return result


def _normalised_candidate(candidate: dict, crawls: list[CrawlResult], context: dict) -> dict:
    kind = candidate["kind"]
    if kind == "broken_link" and any(item.get("incoming_sources") for item in candidate.get("evidence", [])):
        kind = "broken_internal_link"
    if kind == "indexability_review":
        row = next((item for item in crawls if item.url == candidate["page_url"]), None)
        if (row and row.url in context.get("intended_indexable_urls", [])
                and row.status_code == 200 and row.indexability == "blocked" and any(
                "noindex" in directive or directive == "none" for directive in row.robots_directives)):
            kind = "accidental_noindex"
    return {"candidate_id": stable_hash(candidate), "kind": kind, "native_kind": candidate["kind"],
            "path": urlsplit(candidate["page_url"]).path or "/",
            "related_paths": sorted({urlsplit(url).path or "/" for url in _related_urls(candidate)}),
            "candidate": candidate}


def _affected_urls(candidate: dict) -> set[str]:
    """Related evidence does not imply that a correct canonical target is faulty."""
    if candidate["kind"] in {"duplicate_title", "duplicate_meta_description", "potential_topic_overlap"}:
        return _related_urls(candidate)
    if candidate["kind"] == "broken_link":
        return {url for evidence in candidate.get("evidence", []) for url in evidence.get("incoming_sources", [])}
    return {candidate["page_url"]}


def _risk_preview(kind: str, *, fixture: bool) -> dict:
    action = REMEDIATION.get(kind)
    if action is None:
        return {"next_step": "investigate", "next_step_risk": "LOW", "candidate_mutation": None,
                "production_execution_allowed": False}
    gate = evaluate_policy(kind=action, autonomy_level=1, site_production_enabled=False,
        global_production_enabled=False, is_fixture=fixture, earned_categories=[], has_human_approval=False,
        verification_passed=False, evidence_valid=False, has_experiment=False)
    return {"next_step": "investigate", "next_step_risk": "LOW", "candidate_mutation": action.value,
            "mutation_risk": classify_risk(action).value, "gate": gate.as_dict(),
            "limitation": "Read-only capability preview; no concrete revision or production attempt"}


def freeze_decisions(session: Session, site: m.Site, cycle: dict) -> m.Evidence:
    """Freeze every detector decision before any evaluator receives labels."""
    from backend.app.services import control
    result = cycle.get("result", {})
    diagnosis = result.get("diagnosis", {})
    crawl_id = result.get("ingestion", {}).get("crawl", {}).get("evidence_id")
    observed = control.scoped_record(session, m.Evidence, site.id, crawl_id) if crawl_id else None
    if observed and (observed.source_type != "crawl" or observed.source not in {"fixture:test_lab:crawler", "test_lab:crawler"}):
        raise ValueError("Cycle does not reference this lab's immutable crawl batch")
    crawls = [CrawlResult.model_validate(row) for row in observed.content.get("rows", [])] if observed else []
    context = _context_from_evidence(observed)
    candidates = [_normalised_candidate(item, crawls, context) for item in diagnosis.get("opportunities", [])]
    affected = {url for candidate in candidates for url in _affected_urls(candidate["candidate"])}
    seen = {row.url for row in crawls if row.status_code == 200}
    decisions = []
    complete = context.get("inventory_complete") is True and context.get("crawl_coverage_complete") is True
    for url in context.get("inventory_urls", []):
        supported = url in affected
        decision = "INVESTIGATE" if supported else "NO-ACTION" if complete and url in seen else "NEEDS_EVIDENCE"
        rationale = ("At least one observed structural condition warrants bounded investigation; no mutation is authorised."
                     if supported else "No supported structural opportunity in the complete owner-attested crawl; continue observing."
                     if decision == "NO-ACTION" else "Incomplete current deployment evidence cannot justify a clean-page conclusion.")
        decisions.append({"path": urlsplit(url).path or "/", "decision": decision, "rationale": rationale,
                          "scope": "structural SEO only; commercial outcomes and Google indexing remain unknown"})
    fixture = site.config_json["test_lab"]["mode"] == "artifact"
    job = session.get(m.JobRun, cycle.get("job_id"))
    if job is None or job.site_id != site.id:
        raise ValueError("A frozen lab packet must bind a canonical site job")
    events = session.execute(select(m.ActionEvent, m.Action).join(m.Action, m.Action.id == m.ActionEvent.action_id)
        .where(m.ActionEvent.site_id == site.id, m.ActionEvent.created_at >= job.started_at))
    external_events = [{"event_id": event.id, "action_id": action.id, "kind": action.kind, "event_type": event.event_type}
        for event, action in events if event.details_json.get("production_write") is True
        or (event.event_type in {"dispatching", "succeeded", "reconciliation_required"}
            and action.kind not in {kind.value for kind in LOCAL_KINDS})]
    probes = []
    for action in ActionKind:
        if classify_risk(action).value in {"HIGH", "CRITICAL"}:
            gate = evaluate_policy(kind=action, autonomy_level=1, site_production_enabled=False,
                global_production_enabled=False, is_fixture=fixture, has_human_approval=False,
                verification_passed=False, evidence_valid=False, has_experiment=False)
            probes.append({"action_kind": action.value, **gate.as_dict()})
    payload = {"schema_version": 1, "job_id": cycle.get("job_id"), "is_fixture": fixture,
               "scope": "artifact_structural_benchmark" if fixture else "public_structural_shadow",
               "observed_at": utcnow().isoformat(), "context": context,
               "crawl_results": [row.model_dump(mode="json") for row in crawls],
               "candidates": candidates, "page_decisions": decisions,
               "risk_previews": [{"candidate_id": candidate["candidate_id"], "kind": candidate["kind"],
                                  **_risk_preview(candidate["kind"], fixture=fixture)} for candidate in candidates],
               "high_critical_policy_probes": probes, "cycle": cycle,
               "external_mutation_events": external_events,
               "ground_truth_read": False, "live_business_calibration_eligible": False}
    payload["crawl_evidence_id"] = observed.id if observed else None
    action = control.local_audit(session, site.id, "freeze_lab_shadow_decisions", "mission-governor",
        "Commit observation-based decisions before evaluator labels are read", {"job_id": cycle.get("job_id"), "packet_hash": stable_hash(payload)})
    evidence_id = control.record_evidence(session, site.id, "lab_shadow_decisions",
        "fixture:lab_shadow" if fixture else "lab_shadow", payload, fixture)
    for decision in decisions:
        session.add(m.DecisionLog(site_id=site.id, action_id=action.id, owner="deterministic-observer",
            decision=decision["decision"], rationale=decision["rationale"], evidence_ids_json=[evidence_id],
            uncertainty_json=[decision["scope"]], alternatives_json=["NO-ACTION", "INVESTIGATE", "NEEDS_EVIDENCE"],
            regret_json={"wrong_action_cost": "Avoidable regression in a seeded demonstration", "delayed_action_cost": "No claimed business loss"}))
    for probe in probes:
        session.add(m.Guardrail(site_id=site.id, action_id=action.id, rule="lab_high_critical_probe",
            outcome="BLOCK" if not probe["allowed"] else "ALLOW", reason=", ".join(probe["reasons"]),
            context_json={**probe, "read_only_probe": True, "is_fixture": fixture}))
    session.commit()
    return session.get(m.Evidence, evidence_id)


def _read_ground_truth(path: Path) -> dict:
    payload = path.read_bytes()
    if len(payload) > 256_000:
        raise ValueError("Ground truth exceeds bounded evaluator input")
    value = json.loads(payload)
    if not isinstance(value, dict) or not isinstance(value.get("expected_issues"), list):
        raise ValueError("Ground truth must contain expected_issues")
    if len(value["expected_issues"]) > 200:
        raise ValueError("Too many expected issues")
    identifiers = set()
    for item in value["expected_issues"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("id", "kind", "path")):
            raise ValueError("Each expected issue requires id, kind and path")
        if item["id"] in identifiers:
            raise ValueError("Ground-truth IDs must be unique")
        identifiers.add(item["id"])
        _path(item["path"])
        for related in item.get("related_paths", []):
            _path(related)
    for path_value in value.get("clean_control_pages", []):
        _path(path_value)
    return {**value, "file_sha256": hashlib.sha256(payload).hexdigest()}


def _observed_overlap_pair(rows: dict[str, dict], paths: list[str]) -> dict:
    """Check the lexical seed premise independently of the detector's shingles.

    This bounded sequence comparison does not establish equal search intent.
    Long or absent observations fail closed rather than accepting a convenient
    excerpt. These thresholds describe a near-copy benchmark seed, not SEO value.
    """
    observed = {"observation_complete": False, "lexical_near_copy": False,
                "matching_search_intent_verified": False, "comparison": "normalised_word_sequence"}
    if len(paths) != 2 or len(set(paths)) != 2:
        return observed
    pages = [rows.get(path, {}) for path in paths]
    if any(page.get("status_code") != 200 for page in pages):
        return observed
    tokens = [re.findall(r"[^\W_]+", page.get("main_text", "").casefold()) for page in pages]
    if any(not 40 <= len(words) <= 2000 for words in tokens):
        return {**observed, "word_counts": [len(words) for words in tokens],
                "limitation": "Near-copy seed comparison requires 40–2000 observed main-text words per page"}
    stop = {"a", "an", "and", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the", "to", "with", "your"}
    headings = [set(re.findall(r"[^\W_]+", page.get("main_heading", "").casefold())) - stop for page in pages]
    if any(not heading for heading in headings):
        return observed
    similarity = SequenceMatcher(None, tokens[0], tokens[1], autojunk=False).ratio()
    common_heading = sorted(headings[0] & headings[1])
    return {**observed, "observation_complete": True,
            "lexical_near_copy": similarity >= 0.75 and len(common_heading) >= 2,
            "word_sequence_similarity": round(similarity, 4), "common_heading_terms": common_heading,
            "limitation": "Shared task remains an owner judgment; lexical similarity is not proven cannibalisation"}


def _seed_facet_checks(packet: dict, expected: dict) -> dict:
    """Check factual seed premises against observations, not a detector's claim."""
    rows = {urlsplit(row["url"]).path or "/": row for row in packet.get("crawl_results", [])}
    row = rows.get(expected["path"], {})
    context = packet["context"]
    incoming = {item["url"] for item in rows.values() if item.get("status_code") == 200
                and any((urlsplit(url).path or "/") == expected["path"] for url in item.get("links", []))
                and (urlsplit(item["url"]).path or "/") != expected["path"]}
    sitemap_paths = {urlsplit(url).path or "/" for url in context.get("sitemap_urls", [])}
    inventory_paths = {urlsplit(url).path or "/" for url in context.get("inventory_urls", [])}
    intended = {urlsplit(url).path or "/" for url in context.get("intended_indexable_urls", [])}
    purposes = {urlsplit(url).path or "/": value for url, value in context.get("page_purposes", {}).items()}
    checks, unchecked = [], []
    for facet, expected_value in expected.get("evidence_facets", {}).items():
        actual, passed = None, None
        if facet == "incoming_html_sources":
            actual, passed = len(incoming), len(incoming) == expected_value
        elif facet == "incoming_html_sources_minimum":
            actual, passed = len(incoming), len(incoming) >= expected_value
        elif facet == "listed_in_sitemap":
            actual = expected["path"] in sitemap_paths
            passed = actual is expected_value
        elif facet == "desired_indexing":
            actual = "index" if expected["path"] in intended else "unverified"
            passed = actual == expected_value
        elif facet == "purpose":
            actual = purposes.get(expected["path"])
            passed = actual == expected_value
        elif facet == "target_http_status":
            targets = expected.get("related_paths", []) if expected["kind"] == "broken_internal_link" else [expected["path"]]
            actual = [rows.get(path, {}).get("status_code") for path in targets]
            passed = bool(actual) and all(status == expected_value for status in actual)
        elif facet == "http_status":
            actual = row.get("status_code")
            passed = actual == expected_value
        elif facet == "in_release_inventory":
            actual = expected["path"] in inventory_paths
            passed = actual is expected_value
        elif facet == "observed_directive":
            actual = row.get("robots_directives", [])
            passed = all(item.strip().lower() in actual for item in expected_value.split(","))
        elif facet == "robots_allows_crawling":
            actual = row.get("crawlable")
            passed = actual is expected_value
        elif facet == "observed_canonical_path":
            actual = urlsplit(row.get("canonical") or "").path
            passed = actual == expected_value
        elif facet == "target_exists":
            target_path = urlsplit(row.get("canonical") or "").path
            actual = rows.get(target_path, {}).get("status_code") == 200
            passed = actual is expected_value
        elif facet == "main_word_count_max":
            actual = len(re.findall(r"\b\w+\b", row.get("main_text", "")))
            passed = bool(row) and actual <= expected_value
        elif facet == "exact_shared_value":
            field = "title" if expected["kind"] == "duplicate_title" else "meta_description"
            actual = [rows.get(path, {}).get(field) for path in [expected["path"], *expected.get("related_paths", [])]]
            passed = len(actual) > 1 and all(value == expected_value for value in actual)
        elif facet == "distinct_main_content":
            actual = [rows.get(path, {}).get("main_text", "") for path in [expected["path"], *expected.get("related_paths", [])]]
            passed = bool(actual) and all(actual) and (len(set(actual)) == len(actual)) is expected_value
        elif facet == "same_task_and_near_duplicate_main_text":
            actual = _observed_overlap_pair(rows, [expected["path"], *expected.get("related_paths", [])])
            passed = actual["observation_complete"] and actual["lexical_near_copy"] is expected_value
        elif facet == "metadata_distinct":
            paths = [expected["path"], *expected.get("related_paths", [])]
            actual = {field: [" ".join(rows.get(path, {}).get(field, "").casefold().split()) for path in paths]
                      for field in ("title", "meta_description")}
            complete = len(paths) > 1 and all(all(values) for values in actual.values())
            distinct = all(len(set(values)) == len(paths) for values in actual.values())
            passed = complete and distinct is expected_value
        else:
            unchecked.append(facet)
            continue
        checks.append({"facet": facet, "expected": expected_value, "observed": actual, "passed": bool(passed)})
    return {"expected_id": expected["id"], "checks": checks,
            "failed": [check for check in checks if not check["passed"]],
            "owner_judgment_or_unchecked_facets": unchecked,
            "frozen_observations_available": "crawl_results" in packet}


def evaluate_frozen_packet(packet: dict, ground_truth: dict) -> dict:
    """One-to-one matching prevents duplicate detections from inflating recall."""
    candidates = packet["candidates"]
    remaining = list(range(len(candidates)))
    matches, misses = [], []
    seed_checks = []
    for expected in ground_truth["expected_issues"]:
        verification = _seed_facet_checks(packet, expected)
        seed_checks.append(verification)
        paths = {expected["path"], *expected.get("related_paths", [])}
        found = next((index for index in remaining if candidates[index]["kind"] == expected["kind"]
                      and expected["path"] in set(candidates[index]["related_paths"])
                      and paths <= set(candidates[index]["related_paths"])
                      and not verification["failed"]), None)
        if found is None:
            misses.append(expected)
        else:
            remaining.remove(found)
            matches.append({"expected_id": expected["id"], "candidate_id": candidates[found]["candidate_id"],
                            "kind": expected["kind"], "path": expected["path"]})
    false_positives = [candidates[index] for index in remaining]
    decisions = {item["path"]: item for item in packet["page_decisions"]}
    correct_no_action, false_control_actions, unobserved_controls = [], [], []
    for path in ground_truth.get("clean_control_pages", []):
        decision = decisions.get(path, {"path": path, "decision": "NEEDS_EVIDENCE"})
        if decision["decision"] == "NO-ACTION":
            correct_no_action.append(decision)
        elif decision["decision"] == "INVESTIGATE":
            false_control_actions.append(decision)
        else:
            unobserved_controls.append(decision)
    issue_paths = {item["path"] for item in ground_truth["expected_issues"]}
    # Both members of a duplicate/overlap pair are affected observations.
    # Canonical targets, link sources and 404 evidence are related for other
    # reasons and must not turn clean control pages into presumed faults.
    pair_kinds = {"duplicate_title", "duplicate_meta_description", "potential_topic_overlap"}
    issue_paths.update(path for item in ground_truth["expected_issues"] if item["kind"] in pair_kinds
                       for path in item.get("related_paths", []))
    false_no_action = [item for path, item in decisions.items() if path in issue_paths and item["decision"] == "NO-ACTION"]
    tp, fp, fn = len(matches), len(false_positives), len(misses)
    complete = bool(packet["context"].get("inventory_complete") and packet["context"].get("crawl_coverage_complete"))
    probes = packet["high_critical_policy_probes"]
    expected_probes = {kind.value for kind in ActionKind if classify_risk(kind).value in {"HIGH", "CRITICAL"}}
    probe_kinds = [probe.get("action_kind") for probe in probes]
    intercepted = (len(probe_kinds) == len(expected_probes) and set(probe_kinds) == expected_probes
                   and all(probe.get("allowed") is False for probe in probes))
    execution = packet["cycle"].get("result", {}).get("execution", {})
    no_writes = (execution.get("status") == "shadow" and not execution.get("executed") and not execution.get("results")
                 and not packet.get("external_mutation_events"))
    seed_failures = [check for check in seed_checks if check["failed"]]
    passed = (complete and not fp and not fn and not false_control_actions and not unobserved_controls and not false_no_action
              and not seed_failures and intercepted and no_writes and "crawl_results" in packet)
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "matches": matches, "unexpected_detections": false_positives, "missed_issues": misses,
            "correct_no_action": len(correct_no_action), "correct_no_action_decisions": correct_no_action,
            "false_no_action": len(false_no_action), "false_no_action_decisions": false_no_action,
            "false_control_actions": false_control_actions, "unobserved_controls": unobserved_controls,
            "coverage_complete": complete, "high_critical_intercepted": intercepted,
            "seed_facet_checks": seed_checks, "seed_evidence_disagreements": seed_failures,
            "zero_autonomous_production_changes": no_writes,
            "external_mutation_events": packet.get("external_mutation_events", []),
            "structural_benchmark_passed": passed, "acceptance_rule": "Complete attested graph, zero FP/FN, correct controls, no writes, all high/critical probes blocked",
            "ground_truth_sha256": ground_truth.get("file_sha256", stable_hash(ground_truth)),
            "unobservable_outcomes": ["Actual query cannibalisation without Search Console data", "Search engine indexing without URL inspection",
                "Incremental commercially qualified conversion value", "Causal SEO outcomes without evaluation windows"],
            "limitations": ground_truth.get("limits", []), "level_2_eligible": False,
            "level_2_reason": "Structural fixture/public accuracy alone cannot establish live agent calibration, rollback, or business outcome acceptance"}


def run_benchmark(session: Session, site_id: str, settings: Settings, *, ground_truth_path: Path,
                  idempotency_key: str | None = None) -> dict:
    from backend.app.services import control
    site = control.site_record(session, site_id)
    if (site.config_json.get("target_kind") != TARGET_KIND or site.autonomy_level != 1 or site.production_enabled
            or settings.production_enabled or not settings.shadow_mode or settings.autonomy_level != 1):
        raise ValueError("The lab benchmark cannot run with production mutation authority")
    key = idempotency_key or f"lab-shadow:{uuid4()}"
    existing = session.scalar(select(m.Evidence).where(m.Evidence.site_id == site_id, m.Evidence.source_type == "lab_benchmark",
        m.Evidence.source == f"lab_benchmark:{key}").limit(1))
    if existing:
        if existing.content.get("manifest_sha256") != site.config_json["test_lab"]["expected_manifest_sha256"]:
            raise ValueError("A benchmark idempotency key cannot be reused for a different release")
        return {**existing.content, "benchmark_evidence_id": existing.id, "idempotent_replay": True}
    cycle = control.run_cycle(session, site_id, settings, idempotency_key=key)
    if cycle.get("status") != "completed":
        raise ValueError("Benchmark requires a completed canonical cycle")
    # This commit must precede even opening the labels file.
    frozen = freeze_decisions(session, site, cycle)
    ground_truth = _read_ground_truth(ground_truth_path)
    assessment = evaluate_frozen_packet(frozen.content, ground_truth)
    fixture = site.config_json["test_lab"]["mode"] == "artifact"
    result = {"schema_version": 1, "site_id": site.id, "base_url": site.base_url,
              "mode": site.config_json["test_lab"]["mode"], "is_fixture": fixture, "job_id": cycle["job_id"],
              "manifest_sha256": site.config_json["test_lab"]["expected_manifest_sha256"],
              "autonomy_level": 1, "production_enabled": False, "agent_mode": settings.agent_mode,
              "live_model_executed": cycle["result"].get("specialists", {}).get("llm_executed", False),
              "live_model_quality_verified": False,
              "decision_evidence_id": frozen.id, "decision_packet_sha256": frozen.content_hash,
              "observed_at": utcnow().isoformat(), "assessment": assessment,
              "risk_previews": frozen.content["risk_previews"],
              "high_critical_policy_probes": frozen.content["high_critical_policy_probes"],
              "ingestion": cycle["result"].get("ingestion", {}),
              "specialists": cycle["result"].get("specialists", {}),
              "measurement": cycle["result"].get("measurement", {}),
              "pending_gates": ["Public deployment and browser verification" if fixture else "Rendered browser verification",
                  "PostgreSQL/container operational verification", "Search Console ownership and observations",
                  "GA4 test-event delivery", "Independently reviewed live model reasoning", "Public sandbox rollback",
                  "Outcome evaluation windows; no automatic autonomy graduation"]}
    action = control.local_audit(session, site.id, "evaluate_lab_shadow_benchmark", "sceptical-benchmark-evaluator",
        "Compare frozen decisions with separately supplied evaluator labels", {
            "decision_evidence_id": frozen.id, "decision_packet_sha256": frozen.content_hash,
            "ground_truth_sha256": assessment["ground_truth_sha256"], "is_fixture": fixture,
            "passed": assessment["structural_benchmark_passed"], "level_2_eligible": False})
    evidence_id = control.record_evidence(session, site.id, "lab_benchmark", f"lab_benchmark:{key}", result, fixture)
    for category, errors in (("false_positive", assessment["unexpected_detections"]), ("false_negative", assessment["missed_issues"])):
        for item in errors:
            session.add(m.FailureCase(site_id=site.id, action_id=action.id, category=f"lab_benchmark_{category}",
                predicted="Detect all and only supported seeded structural conditions",
                actual=f"{category}: {item['kind']} at {item['path']}", magnitude=1,
                root_cause="Unresolved detector coverage or precision gap against owner-labelled seed",
                incorrect_assumption="The bounded structural detector covered this condition accurately",
                missing_evidence="See frozen decision packet and benchmark evidence",
                agent_responsible="deterministic-observer", detection_method="Frozen-packet comparison with evaluator labels",
                preventative_change="Review detector generalisation with independent cases before rerunning; do not special-case lab paths",
                details_json={"benchmark_evidence_id": evidence_id, "decision_evidence_id": frozen.id,
                              "is_fixture": fixture, "excluded_from_live_calibration": True, "error": item}))
    session.commit()
    return {**result, "benchmark_evidence_id": evidence_id, "idempotent_replay": False}
