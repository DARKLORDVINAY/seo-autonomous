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
RESERVED_OBSERVATION_KEYS = frozenset({
    "answer_key", "answers", "autonomy_level", "benchmark_labels", "budget", "budgets",
    "clean_control_pages", "expected_decisions", "expected_issues", "expected_outcomes",
    "family", "ground_truth", "guardrail", "guardrails", "label", "labels", "level_2_eligible",
    "paid_api_calls", "policy", "private_case_results", "private_label", "production_enabled",
    "production_write_budget", "production_writes", "scoring_key", "stratum", "system_prompt",
    "tool_permissions", "truth",
})
RESERVED_OBSERVATION_KEY_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]+", "", key.casefold()) for key in RESERVED_OBSERVATION_KEYS
)
MAX_STRUCTURED_OBSERVATION_NODES = 100_000


def _reject_reserved_observation_keys(value: Any) -> None:
    """Reject label/authority channels recursively without interpreting prose."""
    pending = [value]
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if nodes > MAX_STRUCTURED_OBSERVATION_NODES:
            raise ValueError("Structured observation node budget exceeded")
        if isinstance(current, dict):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise ValueError("Structured observation keys must be strings")
                normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
                if normalized in RESERVED_OBSERVATION_KEY_TOKENS:
                    raise ValueError("Observation contains an evaluator-private or authority key")
                pending.append(nested)
        elif isinstance(current, list):
            pending.extend(current)


def _validate_case(value: Any) -> tuple[list[CrawlResult], AnalysisContext, list[GSCRow], list[GA4Row], str]:
    if not isinstance(value, dict) or set(value) - CASE_FIELDS:
        raise ValueError("Only observation fields are accepted")
    _reject_reserved_observation_keys(value)
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
    observed = {normalise_url(crawl.url, ctx.site_url): crawl for crawl in crawls}
    intended = {normalise_url(url, ctx.site_url) for url in ctx.intended_indexable_urls}
    unexpected_live = any(
        url not in inventory and crawl.status_code == 200
        and normalise_url(crawl.final_url, ctx.site_url) == url
        for url, crawl in observed.items()
    )

    def sufficient(crawl: CrawlResult, required_indexable: bool) -> bool:
        if not required_indexable:
            # A trusted non-indexable/private inventory item does not require
            # fetching content that robots intentionally protects. Only an
            # explicit block or a terminal HTTP observation is sufficient.
            if crawl.crawlable is False and not any(
                issue.get("kind") not in {"robots_blocked"} for issue in crawl.issues
            ):
                return True
            return crawl.status_code in {200, 404, 410} and not crawl.issues
        return (
            crawl.status_code in {200, 404, 410}
            and not crawl.issues
            and (crawl.status_code != 200 or (
                crawl.crawlable is True and crawl.indexability != "unknown"
                and (crawl.main_content_observed or bool(crawl.main_text))
            ))
        )

    return bool(
        inventory and "" not in inventory and len(observed) == len(crawls)
        and inventory <= observed.keys() and not unexpected_live
        and ctx.inventory_complete and ctx.crawl_coverage_complete and ctx.sitemap_complete
        # Unknown/new parser flags are not silently classified as complete
        # evidence. Trusted intent can narrow the content-observation scope,
        # but untrusted page text cannot.
        and all(sufficient(observed[url], not intended or url in intended) for url in inventory)
    )


def _related(candidate: dict[str, Any]) -> list[str]:
    if candidate["kind"] == "broken_internal_link":
        # Broken-link identity is the observed source->destination edge.
        return sorted({evidence["target_url"] for evidence in candidate["evidence"]
                       if isinstance(evidence.get("target_url"), str)})
    # A canonical target is context, not another affected page. Only genuine
    # grouped findings expose several affected URLs to the evaluator.
    if candidate["kind"] not in {
        "canonical_cycle", "duplicate_title", "duplicate_meta_description",
        "potential_topic_overlap", "cannibalisation_hypothesis",
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


def validate_observation_cases(cases: Any) -> list[dict[str, Any]]:
    """Validate a bounded observation-only challenge without scoring it.

    Evaluator-private labels and authority fields are deliberately outside this
    contract.  Returning the original JSON-compatible packets (rather than the
    parsed domain objects) keeps the byte commitment stable across processes.
    """
    if not isinstance(cases, list) or not 0 < len(cases) <= MAX_CASES:
        raise ValueError("Case collection budget exceeded")
    identifiers = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if len(identifiers) != len(cases) or any(not isinstance(item, str) for item in identifiers):
        raise ValueError("Case identifiers required")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Case identifiers must be unique")
    for case in cases:
        _validate_case(case)
    return cases


def predict_cases(cases: Any) -> dict[str, Any]:
    validate_observation_cases(cases)
    return {
        "schema_version": 2, "cases": [predict_case(case) for case in cases],
        "autonomy_level": 1, "production_enabled": False,
        "production_write_budget": 0, "production_writes": 0, "paid_api_calls": 0,
        "live_model_executed": False, "level_2_eligible": False,
        "scope": "offline_deterministic_fixture_review_not_live_agent_or_business_validation",
    }
