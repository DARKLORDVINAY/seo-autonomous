"""Bounded, truth-blind fixture adapter over the production SEO detector.

This is not a second detector, live-model evaluation, or an authority pathway.
The evaluator, case generator, network, database and executor are unavailable in
the isolated worker. REVIEW means investigate, not a proven defect or a fix.
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.app.contracts import CrawlResult, GA4Row, GSCRow
from backend.app.seo.analysis import AnalysisContext, analyze, normalise_url

MAX_CASES = 256
MAX_PAGES_PER_CASE = 64
MAX_ROWS_PER_CASE = 512
MAX_CASE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES_PER_CASE = 2048
CONTEXT_FIELDS = frozenset({
    "site_url", "inventory_urls", "inventory_complete", "crawl_coverage_complete",
    "entrypoint_urls", "sitemap_urls", "sitemap_complete", "intended_indexable_urls",
    "page_purposes", "thin_content_word_threshold",
})
CASE_FIELDS = frozenset({"case_id", "crawls", "context", "gsc_rows", "ga4_rows", "rendered_crawls"})
def _validate_case(value: Any) -> tuple[list[CrawlResult], AnalysisContext, list[GSCRow], list[GA4Row], str]:
    if not isinstance(value, dict) or set(value) - CASE_FIELDS:
        raise ValueError("Only observation fields are accepted")
    if not isinstance(value.get("case_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value["case_id"]):
        raise ValueError("Opaque case identifier required")
    if len(json.dumps(value, allow_nan=False).encode()) > MAX_CASE_BYTES:
        raise ValueError("Case byte budget exceeded")
    context = value.get("context", {})
    if not isinstance(context, dict) or set(context) - CONTEXT_FIELDS:
        raise ValueError("Context cannot carry policy, budgets, labels or evaluator data")
    ctx = AnalysisContext.model_validate(context)
    for key in ("crawls", "rendered_crawls", "gsc_rows", "ga4_rows"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > (MAX_PAGES_PER_CASE if "crawl" in key else MAX_ROWS_PER_CASE):
            raise ValueError("Observation budget exceeded")
    crawls = [CrawlResult.model_validate(item) for item in value.get("crawls", [])]
    if len({crawl.url for crawl in crawls}) != len(crawls):
        raise ValueError("Duplicate raw observation identity")
    by_url = {crawl.url: crawl for crawl in crawls}
    rendered_seen: set[str] = set()
    for item in value.get("rendered_crawls", []):
        rendered = CrawlResult.model_validate(item)
        raw = by_url.get(rendered.url)
        if (not raw or rendered.url in rendered_seen or raw.final_url != rendered.final_url
                or raw.status_code != rendered.status_code):
            raise ValueError("Rendered DOM fixture cannot invent a different HTTP response")
        rendered_seen.add(rendered.url)
        by_url[rendered.url] = rendered
    crawls = list(by_url.values())
    gsc = [GSCRow.model_validate(item) for item in value.get("gsc_rows", [])]
    ga4 = [GA4Row.model_validate(item) for item in value.get("ga4_rows", [])]
    return crawls, ctx, gsc, ga4, "simulated_dom_fixture" if rendered_seen else "raw_crawl_fixture"


def _coverage(crawls: list[CrawlResult], ctx: AnalysisContext) -> bool:
    inventory = {normalise_url(url, ctx.site_url) for url in ctx.inventory_urls}
    observed = {normalise_url(crawl.url, ctx.site_url) for crawl in crawls}
    return bool(
        inventory and "" not in inventory and inventory <= observed
        and ctx.inventory_complete and ctx.crawl_coverage_complete and ctx.sitemap_complete
        and all(crawl.status_code in {200, 404, 410}
                # Unknown/new parser flags are not silently classified as
                # complete evidence. They require review, even if no current
                # detector understands their meaning.
                and not crawl.issues
                and (crawl.status_code != 200 or (
                    crawl.crawlable is True and crawl.indexability != "unknown"
                    and (crawl.main_content_observed or bool(crawl.main_text))
                )) for crawl in crawls)
    )


def _related(candidate: dict[str, Any]) -> list[str]:
    # A canonical target is context, not another affected page. Only genuine
    # grouped findings expose several affected URLs to the evaluator.
    if candidate["kind"] not in {
        "duplicate_title", "duplicate_meta_description", "potential_topic_overlap", "cannibalisation_hypothesis",
    }:
        return []
    urls = set()
    for evidence in candidate["evidence"]:
        for page in evidence.get("pages", []):
            url = page if isinstance(page, str) else page.get("url") if isinstance(page, dict) else None
            if isinstance(url, str) and url != candidate["page_url"]:
                urls.add(url)
    return sorted(urls)


def predict_case(value: Any) -> dict[str, Any]:
    identifier = value.get("case_id", "invalid") if isinstance(value, dict) else "invalid"
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
        identifier = "invalid"
    try:
        crawls, context, gsc, ga4, scope = _validate_case(value)
        candidates = [candidate.model_dump(mode="json") for candidate in analyze(gsc, ga4, crawls, context)]
        if len(candidates) > MAX_CANDIDATES_PER_CASE:
            raise ValueError("Candidate budget exceeded")
        complete = _coverage(crawls, context)
        for candidate in candidates:
            candidate.update(disposition="REVIEW", related_urls=_related(candidate))
        return {
            "case_id": identifier, "candidates": candidates,
            "decision": "INVESTIGATE" if candidates else "NO-ACTION" if complete else "NEEDS_EVIDENCE",
            "coverage_complete": complete, "observation_scope": scope,
            "uncertainty": ["fixture_only", "business_effect_unmeasured", "no_live_model_reasoning"]
            + ([] if complete else ["incomplete_observation_coverage"]),
            "error": None,
        }
    except Exception as exc:
        # Do not echo hostile values, exception messages or an incomplete partial
        # result. A contained processing failure cannot earn a clean decision.
        return {"case_id": identifier, "candidates": [], "decision": "NEEDS_EVIDENCE",
                "coverage_complete": False, "observation_scope": "invalid_or_failed_fixture",
                "uncertainty": ["fixture_processing_failed"], "error": type(exc).__name__}


def predict_cases(cases: Any) -> dict[str, Any]:
    if not isinstance(cases, list) or not 0 < len(cases) <= MAX_CASES:
        raise ValueError("Case collection budget exceeded")
    identifiers = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if len(identifiers) != len(cases) or any(not isinstance(item, str) for item in identifiers):
        raise ValueError("Case identifiers required")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Case identifiers must be unique")
    return {
        "schema_version": 2, "cases": [predict_case(case) for case in cases],
        "autonomy_level": 1, "production_enabled": False,
        "production_write_budget": 0, "production_writes": 0, "paid_api_calls": 0,
        "live_model_executed": False, "level_2_eligible": False,
        "scope": "offline_deterministic_fixture_review_not_live_agent_or_business_validation",
    }
