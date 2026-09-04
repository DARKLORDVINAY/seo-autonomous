"""Pure SEO analysis. Computers detect; these functions never authorise changes.

Scores without configured business values are *diagnostic triage scores*, not
predicted revenue. Query-level GSC observations do not prove complete coverage,
indexing, unique people, or causal effects. All tunable thresholds are engineering
heuristics, not claims about universal search behaviour.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import Field, model_validator

from backend.app.contracts import CrawlResult, GA4Row, GSCRow, OpportunityCandidate, StrictModel


class AnalysisContext(StrictModel):
    site_url: str | None = None
    inventory_urls: list[str] = Field(default_factory=list)
    inventory_complete: bool = False
    crawl_coverage_complete: bool = False
    entrypoint_urls: list[str] = Field(default_factory=list)
    sitemap_urls: list[str] = Field(default_factory=list)
    sitemap_complete: bool = False
    intended_indexable_urls: list[str] = Field(default_factory=list)
    page_purposes: dict[str, str] = Field(default_factory=dict)
    thin_content_word_threshold: int = Field(default=80, ge=1, le=500)
    business_values: dict[str, float] = Field(default_factory=dict)
    conversion_value_mapping_verified: bool = False
    min_impressions: int = Field(default=100, ge=1)
    min_ctr_impressions: int = Field(default=100, ge=1)
    ctr_drop_fraction: float = Field(default=0.25, gt=0, lt=1)
    ctr_position_tolerance: float = Field(default=1.0, ge=0)
    decay_fraction: float = Field(default=0.25, gt=0, lt=1)
    cannibalisation_min_share: float = Field(default=0.2, gt=0, le=0.5)
    window_days: int = Field(default=28, ge=7)
    baseline_start: date | None = None
    baseline_end: date | None = None
    current_start: date | None = None
    current_end: date | None = None
    gsc_complete_dates: list[date] = Field(default_factory=list)
    brand_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_windows_and_values(self) -> "AnalysisContext":
        windows = [self.baseline_start, self.baseline_end, self.current_start, self.current_end]
        if any(windows) and not all(windows):
            raise ValueError("Supply all four comparison window boundaries, or none")
        if all(windows):
            assert self.baseline_start is not None and self.baseline_end is not None
            assert self.current_start is not None and self.current_end is not None
            if not self.baseline_start <= self.baseline_end < self.current_start <= self.current_end:
                raise ValueError("Comparison windows must be ordered and non-overlapping")
        for value in self.business_values.values():
            if not math.isfinite(value) or value < 0:
                raise ValueError("Business priority values must be finite and non-negative")
        return self


def _context(value: AnalysisContext | dict[str, Any] | None) -> AnalysisContext:
    return value if isinstance(value, AnalysisContext) else AnalysisContext.model_validate(value or {})


def normalise_url(url: str, site_url: str | None = None) -> str:
    """Drop fragments, retain meaningful query/path distinctions, normalise host."""
    parsed = urlsplit(urljoin(site_url or "", url))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    # Invalid ports are untrusted data, not reasons to crash the analysis loop.
    try:
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = (parsed.scheme.lower() == "https" and port == 443) or (parsed.scheme.lower() == "http" and port == 80)
    netloc = host + (f":{port}" if port and not default_port else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _same_site(url: str, site_url: str | None) -> bool:
    if not site_url:
        return True
    return urlsplit(url).netloc == urlsplit(normalise_url(site_url)).netloc


def _normalised_intended_urls(context: AnalysisContext) -> set[str]:
    """Return only trusted caller-declared indexing intent.

    Page markup is untrusted observation data and cannot add itself to this set.
    An empty set means intent was not supplied, not that no page is intended.
    """
    return {
        url
        for value in context.intended_indexable_urls
        if (url := normalise_url(value, context.site_url)) and _same_site(url, context.site_url)
    }


def _normalised_purposes(context: AnalysisContext) -> dict[str, str]:
    return {
        url: re.sub(r"[^a-z0-9]+", "_", purpose.casefold()).strip("_")
        for value, purpose in context.page_purposes.items()
        if (url := normalise_url(value, context.site_url))
    }


def _purpose_has_any(purpose: str, markers: set[str]) -> bool:
    tokens = set(filter(None, purpose.split("_")))
    return bool(tokens & markers or any(marker in purpose for marker in markers if "_" in marker))


def calculate_opportunity_score(
    expected_impact: float,
    confidence: float,
    business_value: float | None = None,
    reversibility: float = 1.0,
    risk: float = 0.0,
    cost: float = 0.0,
) -> dict[str, Any]:
    """Return inspectable score components; unknown value is never imputed as money.

    Impact, configured business priority, risk and cost use relative decision
    units. Callers must calibrate them against actual outcomes. The unvalued
    fallback orders investigations only and must not authorise execution.
    """
    values = [expected_impact, confidence, reversibility, risk, cost]
    if business_value is not None:
        values.append(business_value)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Score components must be finite and non-negative")
    if not 0 <= confidence <= 1 or not 0 <= reversibility <= 1:
        raise ValueError("Confidence and reversibility must be probabilities")
    components = {
        "expected_impact": expected_impact,
        "confidence": confidence,
        "reversibility": reversibility,
        "risk": risk,
        "cost": cost,
    }
    flags: list[str] = []
    if business_value is None:
        multiplier = 1.0
        basis = "diagnostic_priority_only"
        flags = ["business_value_unknown", "score_is_diagnostic_priority_only"]
    else:
        components["business_value"] = business_value
        multiplier = business_value
        basis = "relative_business_priority_not_revenue_forecast"
    score = expected_impact * confidence * multiplier * reversibility - risk - cost
    if not math.isfinite(score):
        raise ValueError("Score overflow")
    return {"score": score, "components": components, "business_value": business_value, "basis": basis, "quality_flags": flags}


def _candidate(
    kind: str,
    page: str,
    finding: str,
    evidence: list[dict[str, Any]],
    context: AnalysisContext,
    *,
    impact: float = 1.0,
    confidence: float = 0.6,
    risk: float = 0.1,
    cost: float = 0.1,
    flags: Iterable[str] = (),
    alternatives: Iterable[str] = (),
) -> OpportunityCandidate:
    score = calculate_opportunity_score(impact, confidence, context.business_values.get(page), risk=risk, cost=cost)
    all_flags = list(flags) + score["quality_flags"]
    if not context.conversion_value_mapping_verified:
        all_flags.append("qualified_conversion_value_mapping_unverified")
    return OpportunityCandidate(
        kind=kind,
        page_url=page,
        finding=finding,
        evidence=evidence + [{
            "claim_type": "ASSUMPTION",
            "scoring_basis": score["basis"],
            "business_value": score["business_value"],
            "alternative_explanations": list(alternatives),
            "predicted_conversion_value": None,
        }],
        components=score["components"],
        score=score["score"],
        confidence=confidence,
        recommended_action="investigate",
        quality_flags=sorted(set(all_flags)),
    )


def query_tokens(query: str) -> frozenset[str]:
    """Conservative Unicode word tokens; no asserted semantic equivalence."""
    return frozenset(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE))


def cluster_queries(queries: Iterable[str], threshold: float = 0.5) -> list[dict[str, Any]]:
    """Deterministic complete-link Jaccard clustering, without transitive chaining.

    Every member must meet the lexical threshold against every other member.
    These are lexical research groups, not evidence of identical search intent.
    """
    if not 0 < threshold <= 1:
        raise ValueError("Jaccard threshold must be in (0, 1]")
    unique = sorted({query.strip() for query in queries if query.strip()}, key=lambda q: (q.casefold(), q))
    groups: list[list[str]] = []
    tokens = {query: query_tokens(query) for query in unique}

    def similarity(left: str, right: str) -> float:
        union = tokens[left] | tokens[right]
        return len(tokens[left] & tokens[right]) / len(union) if union else float(left == right)

    for query in unique:
        compatible = [group for group in groups if all(similarity(query, member) >= threshold for member in group)]
        if compatible:
            # Stable tie break; favour the most similar complete-link group.
            selected = max(compatible, key=lambda group: min(similarity(query, member) for member in group))
            selected.append(query)
        else:
            groups.append([query])
    return [{"cluster_id": f"lexical-{index + 1}", "queries": group, "method": "complete_link_jaccard", "threshold": threshold,
             "claim_type": "INFERENCE", "intent_verified": False}
            for index, group in enumerate(groups)]


def _valid_gsc(rows: Iterable[GSCRow]) -> list[GSCRow]:
    """Deduplicate identical observation grains; reject contradictory provider data."""
    seen: dict[tuple[Any, ...], GSCRow] = {}
    for row in rows:
        if row.clicks > row.impressions or not math.isfinite(row.position):
            raise ValueError("Inconsistent GSC observation: clicks exceed impressions or position is non-finite")
        key = (row.date, row.page, row.query, row.country, row.device)
        if key in seen and seen[key] != row:
            raise ValueError("Conflicting GSC rows at the same observation grain")
        seen[key] = row
    return list(seen.values())


def _aggregate_gsc(rows: Iterable[GSCRow], by_query: bool = True) -> dict[tuple[str, ...], dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in _valid_gsc(rows):
        key = (row.page, row.query, row.country, row.device) if by_query else (row.page,)
        aggregate = grouped.setdefault(key, {"clicks": 0, "impressions": 0, "position_total": 0.0, "dates": set(), "data_states": set()})
        aggregate["clicks"] += row.clicks
        aggregate["impressions"] += row.impressions
        aggregate["position_total"] += row.position * row.impressions
        aggregate["dates"].add(row.date)
        aggregate["data_states"].add(row.data_state)
    for aggregate in grouped.values():
        count = aggregate["impressions"]
        aggregate["position"] = aggregate["position_total"] / count if count else None
        aggregate["ctr"] = aggregate["clicks"] / count if count else None
    return grouped


def _dates(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


def _split_windows(rows: list[GSCRow], context: AnalysisContext) -> tuple[list[GSCRow], list[GSCRow], list[str]]:
    if not rows:
        return [], [], ["gsc_unavailable"]
    if context.current_end is not None:
        assert context.current_start is not None and context.baseline_start is not None and context.baseline_end is not None
        start, end = context.current_start, context.current_end
        base_start, base_end = context.baseline_start, context.baseline_end
    else:
        end = max(row.date for row in rows)
        start = end - timedelta(days=context.window_days - 1)
        base_end = start - timedelta(days=1)
        base_start = base_end - timedelta(days=context.window_days - 1)
    base_dates, current_dates = _dates(base_start, base_end), _dates(start, end)
    flags: list[str] = []
    if len(base_dates) != len(current_dates) or len(base_dates) % 7:
        flags.append("non_comparable_windows")
    baseline = [row for row in rows if row.date in base_dates]
    current = [row for row in rows if row.date in current_dates]
    # A non-final date is not made complete by the mere presence of a row.
    final_dates = set(context.gsc_complete_dates)
    if not (base_dates | current_dates) <= final_dates:
        flags.append("gsc_completeness_unverified")
    if any(row.data_state != "final" for row in baseline + current):
        flags.append("partial_or_unknown_gsc_data")
    return baseline, current, flags


def data_quality_report(gsc_rows: list[GSCRow], ga4_rows: list[GA4Row], context: AnalysisContext | dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = _context(context)
    flags: set[str] = set()
    if not gsc_rows:
        flags.add("gsc_unavailable_not_zero")
    if not ga4_rows:
        flags.add("ga4_unavailable_not_zero")
    if any(row.data_state != "final" for row in gsc_rows):
        flags.add("partial_or_unknown_gsc_data")
    if not ctx.conversion_value_mapping_verified:
        flags.add("qualified_conversion_value_mapping_unverified")
    if any(row.qualified_conversions is None or row.conversion_value is None for row in ga4_rows):
        flags.add("qualified_conversion_outcome_missing")
    flags.update(flag for row in ga4_rows for flag in row.quality_flags)
    return {"quality_flags": sorted(flags), "gsc_rows": len(gsc_rows), "ga4_rows": len(ga4_rows),
            "zero_observations_is_not_zero_business": True, "claim_type": "FACT"}


def detect_high_impression_positions(gsc_rows: list[GSCRow], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    found = []
    for key, aggregate in sorted(_aggregate_gsc(gsc_rows).items()):
        if aggregate["impressions"] < ctx.min_impressions or aggregate["position"] is None or not 4 <= aggregate["position"] <= 15:
            continue
        flags = ["average_position_is_not_a_fixed_rank"]
        if aggregate["data_states"] != {"final"}:
            flags.append("partial_or_unknown_gsc_data")
        found.append(_candidate("visibility_opportunity", key[0], "Observed impressions at average positions 4–15 warrant intent and business-fit review.",
            [{"source": "gsc", "claim_type": "FACT", "query": key[1], "country": key[2], "device": key[3],
              "impressions": aggregate["impressions"], "clicks": aggregate["clicks"], "position": aggregate["position"],
              "dates": sorted(d.isoformat() for d in aggregate["dates"])}], ctx,
            impact=math.log1p(aggregate["impressions"]), confidence=0.55 if len(flags) == 1 else 0.3, flags=flags,
            alternatives=["Non-commercial query intent", "Low qualified conversion propensity", "Mixed SERP layouts or position distributions"]))
    return found


def detect_ctr_anomaly(
    gsc_rows: list[GSCRow],
    baseline_rows: list[GSCRow] | None = None,
    context: AnalysisContext | dict[str, Any] | None = None,
) -> list[OpportunityCandidate]:
    ctx = _context(context)
    if baseline_rows is None:
        baseline, current, flags = _split_windows(_valid_gsc(gsc_rows), ctx)
    else:
        baseline, current = _valid_gsc(baseline_rows), _valid_gsc(gsc_rows)
        flags = []
        if not baseline or not current:
            return []
        # Explicit rows still require dates certified by ingestion metadata.
        explicit = ctx.model_copy(update={
            "baseline_start": min(row.date for row in baseline), "baseline_end": max(row.date for row in baseline),
            "current_start": min(row.date for row in current), "current_end": max(row.date for row in current),
        })
        if explicit.baseline_end >= explicit.current_start:
            return []
        _, _, flags = _split_windows(baseline + current, explicit)
    if flags:
        return []
    before, after = _aggregate_gsc(baseline), _aggregate_gsc(current)
    found = []
    for key in sorted(before.keys() & after.keys()):
        base, now = before[key], after[key]
        if min(base["impressions"], now["impressions"]) < ctx.min_ctr_impressions or base["ctr"] <= 0:
            continue
        if abs(base["position"] - now["position"]) > ctx.ctr_position_tolerance:
            continue
        if now["ctr"] >= base["ctr"] * (1 - ctx.ctr_drop_fraction):
            continue
        found.append(_candidate("ctr_anomaly", key[0], "CTR is lower than a matched historical query/device/country baseline at comparable average position.",
            [{"source": "gsc", "claim_type": "FACT", "query": key[1], "country": key[2], "device": key[3],
              "baseline_ctr": base["ctr"], "current_ctr": now["ctr"], "baseline_impressions": base["impressions"],
              "current_impressions": now["impressions"], "baseline_position": base["position"], "current_position": now["position"],
              "threshold_is_configurable_heuristic": True}], ctx,
            impact=math.log1p(now["impressions"] * (base["ctr"] - now["ctr"])), confidence=0.55,
            flags=["exploratory_multiple_comparisons", "not_a_significance_test"],
            alternatives=["SERP layout change", "Demand or audience mix change", "Search engine title rewrite", "Seasonality", "Query coverage changes"]))
    return found


def detect_content_decay(gsc_rows: list[GSCRow], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    baseline, current, flags = _split_windows(_valid_gsc(gsc_rows), ctx)
    if flags:
        return []
    before, after = _aggregate_gsc(baseline, by_query=False), _aggregate_gsc(current, by_query=False)
    found = []
    for key in sorted(before.keys() & after.keys()):
        base, now = before[key], after[key]
        if base["impressions"] < ctx.min_impressions or base["clicks"] == 0:
            continue
        if now["clicks"] >= base["clicks"] * (1 - ctx.decay_fraction):
            continue
        click_change = now["clicks"] / base["clicks"] - 1
        impression_change = now["impressions"] / base["impressions"] - 1
        found.append(_candidate("content_decay_hypothesis", key[0], "Clicks declined in complete comparable windows; content decay is one possible explanation.",
            [{"source": "gsc", "claim_type": "FACT", "baseline_clicks": base["clicks"], "current_clicks": now["clicks"],
              "click_change": click_change, "impression_change": impression_change,
              "baseline_position": base["position"], "current_position": now["position"],
              "baseline_dates": sorted(d.isoformat() for d in base["dates"]), "current_dates": sorted(d.isoformat() for d in now["dates"])}], ctx,
            impact=math.log1p(base["clicks"] - now["clicks"]), confidence=0.5,
            flags=["content_cause_unproven", "exploratory_multiple_comparisons", "query_row_coverage_may_change"],
            alternatives=["Seasonality or lower search demand", "Ranking loss", "SERP layout or CTR change", "Cannibalisation", "Indexing problem", "GSC query suppression"]))
    return found


def detect_cannibalisation(gsc_rows: list[GSCRow], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    groups: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    group_states: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in _valid_gsc(gsc_rows):
        if not row.query.strip():
            continue
        key = (row.query.casefold().strip(), row.country, row.device)
        groups[key][row.page] += row.impressions
        group_states[key].add(row.data_state)
    found = []
    for key, pages in sorted(groups.items()):
        total = sum(pages.values())
        if total < ctx.min_impressions:
            continue
        contenders = sorted((page, count) for page, count in pages.items() if count / total >= ctx.cannibalisation_min_share)
        if len(contenders) < 2:
            continue
        flags = ["cannibalisation_is_hypothesis", "no_merge_without_intent_and_outcome_evidence"]
        if group_states[key] != {"final"}:
            flags.append("partial_or_unknown_gsc_data")
        found.append(_candidate("cannibalisation_hypothesis", contenders[0][0], "Several pages receive impressions for the same query; harmful cannibalisation is unproven.",
            [{"source": "gsc", "claim_type": "FACT", "query": key[0], "country": key[1], "device": key[2],
              "pages": [{"url": page, "impressions": count, "share": count / total} for page, count in contenders]}], ctx,
            impact=math.log1p(total), confidence=0.35, risk=0.3, flags=flags,
            alternatives=["Distinct legitimate intents", "Sitelinks or multiple useful results", "Page migration over time", "Locale or product variants"]))
    return found


def detect_orphan_pages(
    crawls: list[CrawlResult],
    inventory_urls: list[str] | None = None,
    context: AnalysisContext | dict[str, Any] | None = None,
) -> list[OpportunityCandidate]:
    ctx = _context(context)
    inventory = {normalise_url(url, ctx.site_url) for url in (inventory_urls if inventory_urls is not None else ctx.inventory_urls)} - {""}
    inventory = {url for url in inventory if _same_site(url, ctx.site_url)}
    if not inventory:
        return []
    intended = _normalised_intended_urls(ctx)
    purposes = _normalised_purposes(ctx)
    graph_inventory = inventory & intended if intended else inventory
    incoming: dict[str, set[str]] = defaultdict(set)
    observed_requests: dict[str, CrawlResult] = {}
    for crawl in crawls:
        requested = normalise_url(crawl.url, ctx.site_url)
        if requested and _same_site(requested, ctx.site_url):
            observed_requests[requested] = crawl
        source = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        if not source or not _same_site(source, ctx.site_url) or crawl.status_code != 200:
            continue
        for link in crawl.links:
            target = normalise_url(link, source)
            if target and target != source and _same_site(target, ctx.site_url):
                incoming[target].add(source)
    covered = (bool(graph_inventory) and ctx.inventory_complete and ctx.crawl_coverage_complete
               and graph_inventory <= observed_requests.keys()
               and not any(_incomplete_graph_observation(observed_requests[url]) for url in graph_inventory))
    entrypoints = {normalise_url(url, ctx.site_url) for url in ctx.entrypoint_urls}
    if ctx.site_url:
        entrypoints.add(normalise_url(ctx.site_url))
    found = []
    for page in sorted(inventory - entrypoints):
        observation = observed_requests.get(page)
        purpose = purposes.get(page, "")
        # Redirect aliases, unavailable resources, intentionally excluded URLs,
        # and non-indexable utility/private pages are not orphan-page defects.
        if (not observation or observation.status_code != 200
                or normalise_url(observation.final_url or observation.url, ctx.site_url) != page
                or observation.indexability == "blocked"
                or (intended and page not in intended)
                or _purpose_has_any(purpose, {"redirect", "retired", "private", "internal_search"})):
            continue
        if incoming.get(page):
            continue
        found.append(_candidate("orphan_page" if covered else "potential_orphan_page", page,
            "No incoming link was found in the inspected internal HTML link graph.",
            [{"source": "crawler_inventory", "claim_type": "FACT", "incoming_observed": 0,
              "inventory_complete": ctx.inventory_complete, "crawl_coverage_complete": ctx.crawl_coverage_complete,
              "observed_inventory_coverage": len(inventory & observed_requests.keys()) / len(inventory),
              "owner_intends_indexable": page in intended if intended else None,
              "full_graph_observed": covered}], ctx,
            confidence=0.85 if covered else 0.3, flags=[] if covered else ["incomplete_graph_cannot_prove_orphan"],
            alternatives=["Links in JavaScript-rendered navigation", "Uncrawled or blocked pages", "Intentionally isolated landing page"]))
    return found


def detect_broken_links(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    incoming: dict[str, set[str]] = defaultdict(set)
    for crawl in crawls:
        if crawl.status_code != 200:
            continue
        source = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        if not source or not _same_site(source, ctx.site_url):
            continue
        for link in crawl.links:
            target = normalise_url(link, source)
            if target and _same_site(target, ctx.site_url):
                incoming[target].add(source)
    found = []
    seen: set[str] = set()
    for crawl in crawls:
        url = normalise_url(crawl.url, ctx.site_url)
        if url in seen or not url or crawl.status_code not in {404, 410, 500, 502, 503, 504}:
            continue
        seen.add(url)
        is_missing = crawl.status_code in {404, 410}
        if is_missing and not incoming.get(url):
            # A missing URL in a sitemap is not evidence of a broken HTML link.
            continue
        if is_missing:
            # The actionable observation grain is the source->destination edge,
            # not the unavailable destination in isolation.
            for source in sorted(incoming[url]):
                found.append(_candidate("broken_internal_link", source,
                    f"An internal link points to a URL that returned HTTP {crawl.status_code}.",
                    [{"source": "crawler", "claim_type": "FACT", "status_code": crawl.status_code,
                      "source_url": source, "target_url": url,
                      "observed_at": crawl.fetched_at.isoformat()}], ctx,
                    impact=1.0, confidence=0.9, flags=["verify_before_change"],
                    alternatives=["Transient response", "Intentional removal without navigation cleanup", "Crawler-specific response"]))
        else:
            found.append(_candidate("server_error", url,
                f"An HTTP {crawl.status_code} response was observed; review availability.",
                [{"source": "crawler", "claim_type": "FACT", "status_code": crawl.status_code,
                  "observed_at": crawl.fetched_at.isoformat(), "incoming_sources": sorted(incoming.get(url, set()))}], ctx,
                impact=1 + math.log1p(len(incoming.get(url, set()))), confidence=0.65,
                flags=["verify_before_change"], alternatives=["Transient failure", "Maintenance", "Crawler-specific response"]))
    return found


def detect_redirect_chains(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    found = []
    for crawl in crawls:
        chain = [normalise_url(url, ctx.site_url) for url in crawl.redirect_chain]
        chain = [url for index, url in enumerate(chain) if url and (index == 0 or chain[index - 1] != url)]
        # Contract: redirect_chain contains all visited URLs including start/end.
        if len(chain) < 3:
            continue
        found.append(_candidate("redirect_chain", crawl.url, "Multiple redirect hops were observed; verify whether a shorter route preserves behaviour.",
            [{"source": "crawler", "claim_type": "FACT", "chain": chain, "hops": len(chain) - 1,
              "loop_observed": len(chain) != len(set(chain))}], ctx,
            confidence=0.9, flags=["redirect_changes_require_human_approval"],
            alternatives=["Required locale or authentication routing", "Temporary deployment routing"]))
    return found


def detect_duplicate_metadata(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    intended = _normalised_intended_urls(ctx)
    found = []
    for field in ("title", "meta_description"):
        groups: dict[str, dict[str, CrawlResult]] = defaultdict(dict)
        for crawl in crawls:
            if crawl.status_code != 200:
                continue
            value = " ".join(getattr(crawl, field).split()).casefold()
            page = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
            if (value and page and crawl.indexability != "blocked"
                    and (not intended or page in intended)):
                groups[value][page] = crawl
        for value, pages in sorted(groups.items()):
            if len(pages) < 2:
                continue
            canonical_targets = {crawl.canonical for crawl in pages.values() if crawl.canonical}
            flags = ["duplicate_metadata_does_not_prove_duplicate_content"]
            if len(canonical_targets) == 1:
                flags.append("shared_canonical_may_be_intentional")
            found.append(_candidate(f"duplicate_{field}", sorted(pages)[0], f"Several observed URLs share the same {field.replace('_', ' ')}.",
                [{"source": "crawler", "claim_type": "FACT", "field": field, "normalised_value": value,
                  "pages": sorted(pages), "canonical_targets": sorted(canonical_targets)}], ctx,
                confidence=0.85, flags=flags, alternatives=["Intentional duplicate URL variants", "Pagination", "Shared brand wording"]))
    return found


def detect_indexability(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    intended = _normalised_intended_urls(ctx)
    found = []
    for crawl in crawls:
        url = normalise_url(crawl.url, ctx.site_url)
        if not url or (intended and url not in intended):
            continue
        robots_blocked = crawl.crawlable is False or any(
            issue.get("kind") == "robots_blocked" for issue in crawl.issues
        )
        noindex_observed = crawl.status_code == 200 and crawl.indexability == "blocked"
        if not noindex_observed and not robots_blocked:
            continue
        found.append(_candidate("indexability_review", url, "Observed crawl or indexability controls conflict with declared public indexing intent.",
            [{"source": "crawler", "claim_type": "FACT", "indexability": crawl.indexability,
              "robots_directives": crawl.robots_directives, "canonical": crawl.canonical,
              "crawlable": crawl.crawlable, "robots_blocked": robots_blocked,
              "owner_intends_indexable": url in intended if intended else None,
              "actual_google_index_status": "unknown"}], ctx,
            confidence=0.65 if noindex_observed else 0.5, risk=0.7,
            flags=["actual_index_status_unknown", "owner_intent_required"],
            alternatives=["Stale owner intent", "Deliberate temporary exclusion", "Crawler-specific robots evaluation"]))
    return found


def _incomplete_graph_observation(crawl: CrawlResult) -> bool:
    return (crawl.status_code not in {200, 404, 410}
            or any(issue.get("kind") in {"link_budget_reached", "fetch_blocked", "robots_unknown", "robots_blocked"}
                   for issue in crawl.issues))


def _inventory_graph(crawls: list[CrawlResult], ctx: AnalysisContext):
    inventory = {normalise_url(url, ctx.site_url) for url in ctx.inventory_urls}
    inventory = {url for url in inventory if url and _same_site(url, ctx.site_url)}
    intended = _normalised_intended_urls(ctx)
    if intended:
        inventory &= intended
    pages, incoming = {}, defaultdict(set)
    observations: dict[str, CrawlResult] = {}
    for crawl in crawls:
        requested = normalise_url(crawl.url, ctx.site_url)
        if requested:
            observations[requested] = crawl
        source = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        if not source or not _same_site(source, ctx.site_url) or crawl.status_code != 200:
            continue
        pages[source] = crawl
        for link in crawl.links:
            target = normalise_url(link, source)
            if target and target != source and _same_site(target, ctx.site_url):
                incoming[target].add(source)
    redirect_alias_touches_inventory = any(
        crawl.redirect_chain
        and normalise_url(crawl.final_url or crawl.url, ctx.site_url) in inventory
        and normalise_url(crawl.url, ctx.site_url) != normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        for crawl in crawls
    )
    complete = bool(inventory and ctx.inventory_complete and ctx.crawl_coverage_complete and inventory <= pages.keys()
                    and inventory <= observations.keys()
                    and not redirect_alias_touches_inventory
                    and not any(_incomplete_graph_observation(observations[url]) for url in inventory))
    return inventory, pages, incoming, complete


def detect_weak_internal_links(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    """One incoming source is a review signal only in an attested complete graph.

    Counting repeated anchors from one page as independent links inflates the
    signal. Orphans have their own detector; short hubs can remain useful.
    """
    ctx = _context(context)
    inventory, pages, incoming, complete = _inventory_graph(crawls, ctx)
    if not complete:
        return []
    intended = _normalised_intended_urls(ctx)
    purposes = _normalised_purposes(ctx)
    entrypoints = {normalise_url(url, ctx.site_url) for url in [*ctx.entrypoint_urls, ctx.site_url or ""]}
    found = []
    for url in sorted(inventory - entrypoints):
        purpose = purposes.get(url, "")
        if (url not in pages or pages[url].indexability == "blocked"
                or (intended and url not in intended)
                or _purpose_has_any(purpose, {"small_collection", "standalone", "single_exercise"})):
            continue
        if len(incoming.get(url, ())) != 1:
            continue
        found.append(_candidate("weak_internal_links", url,
            "Only one distinct internal HTML page links to this inventoried page; review whether another relevant path would help users.",
            [{"source": "crawler_inventory", "claim_type": "FACT", "incoming_sources": sorted(incoming[url]),
              "incoming_observed": 1, "full_graph_observed": True, "indexability": pages[url].indexability}], ctx,
            confidence=0.65, risk=0.2, flags=["link_count_is_not_user_value", "relevance_review_required"],
            alternatives=["A deliberately sequential guide", "A niche reference needing only one entry", "Rendered links not present in HTML"]))
    return found


def _canonical_cycles(crawls: list[CrawlResult], ctx: AnalysisContext) -> list[list[str]]:
    """Return deterministic cycles in the observed one-canonical-per-page graph."""
    edges: dict[str, str] = {}
    for crawl in crawls:
        source = normalise_url(crawl.url, ctx.site_url)
        target = normalise_url(crawl.canonical or "", source) if crawl.canonical else ""
        if (crawl.status_code == 200 and source and target and source != target
                and not crawl.redirect_chain and _same_site(source, ctx.site_url)
                and _same_site(target, ctx.site_url)):
            edges[source] = target
    cycles: set[tuple[str, ...]] = set()
    completed: set[str] = set()
    for start in sorted(edges):
        path: list[str] = []
        positions: dict[str, int] = {}
        node = start
        while node in edges and node not in completed:
            if node in positions:
                cycle = path[positions[node]:]
                pivot = min(range(len(cycle)), key=cycle.__getitem__)
                cycles.add(tuple(cycle[pivot:] + cycle[:pivot]))
                break
            positions[node] = len(path)
            path.append(node)
            node = edges[node]
        completed.update(path)
    return [list(cycle) for cycle in sorted(cycles)]


def detect_canonical_cycles(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    ctx = _context(context)
    found = []
    for pages in _canonical_cycles(crawls, ctx):
        found.append(_candidate("canonical_cycle", pages[0],
            "Observed canonical declarations form a directed cycle with no stable representative URL.",
            [{"source": "crawler", "claim_type": "FACT", "pages": pages,
              "cycle_edges": [{"source": page, "target": pages[(index + 1) % len(pages)]}
                              for index, page in enumerate(pages)],
              "actual_google_canonical": "unknown"}], ctx,
            confidence=0.95, risk=0.9,
            flags=["canonical_change_requires_human_review"],
            alternatives=["Inconsistent deployment snapshots", "Crawler-specific markup variation"]))
    return found


def detect_canonical_mismatch(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    """Observe unexplained non-self canonicals without assuming they are errors."""
    ctx = _context(context)
    pages = {normalise_url(c.url, ctx.site_url): c for c in crawls}
    intended = _normalised_intended_urls(ctx)
    cycle_members = {page for cycle in _canonical_cycles(crawls, ctx) for page in cycle}
    found, seen = [], set()
    for crawl in crawls:
        source = normalise_url(crawl.url, ctx.site_url)
        target = normalise_url(crawl.canonical or "", source) if crawl.canonical else ""
        if (crawl.status_code != 200 or not source or not target or source == target or source in seen
                or source in cycle_members or crawl.redirect_chain or (intended and source not in intended)):
            continue
        seen.add(source)
        target_page = pages.get(target)
        found.append(_candidate("canonical_mismatch", source,
            "This owner-intended page declares a different canonical URL; compare intent and content before proposing any change.",
            [{"source": "crawler", "claim_type": "FACT", "canonical_target": target,
              "pages": [source, target], "target_status": target_page.status_code if target_page else None,
              "cross_host_canonical": urlsplit(source).netloc != urlsplit(target).netloc,
              "owner_intends_indexable": source in intended if intended else None, "canonical_error_proven": False,
              "actual_google_canonical": "unknown"}], ctx, confidence=0.65, risk=0.9,
            flags=["canonical_change_requires_human_review", "nonself_canonical_can_be_intentional"],
            alternatives=["Intentional duplicate consolidation", "Print or parameter variant", "Migration still in progress"]))
    return found


def detect_thin_content(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    """Flag sparse informational content for review, never infer quality from length.

    A working utility, form, short answer, or hub need not meet a prose quota.
    Purpose comes only from the caller's trusted inventory, not a page instruction.
    """
    ctx = _context(context)
    purposes = _normalised_purposes(ctx)
    intended = _normalised_intended_urls(ctx)
    found = []
    for crawl in crawls:
        url = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        requested = normalise_url(crawl.url, ctx.site_url)
        if (crawl.status_code != 200 or not crawl.main_content_observed
                or requested != url or (intended and url not in intended)):
            continue
        purpose = purposes.get(url, "unknown")
        if _purpose_has_any(purpose, {
            "utility", "exercise", "hub", "home", "tool", "calculator", "converter",
            "definition", "dictionary", "glossary", "short_answer", "search_results",
        }):
            continue
        if purpose == "unknown" and crawl.has_interactive_content:
            continue
        words = len(re.findall(r"[^\W_]+", crawl.main_text, flags=re.UNICODE))
        if words >= ctx.thin_content_word_threshold:
            continue
        found.append(_candidate("thin_content", url,
            "The observed main content is sparse; check whether it fulfils its stated purpose before considering a change.",
            [{"source": "crawler", "claim_type": "FACT", "main_word_count": words,
              "review_threshold": ctx.thin_content_word_threshold, "purpose": purpose,
              "quality_judgement": "unknown", "minimum_google_word_count": None}], ctx,
            confidence=0.45, flags=["word_count_is_not_content_quality", "no_generation_without_unique_useful_information"],
            alternatives=["A complete concise answer", "An intentionally brief note", "Meaningful media or interactive content"]))
    return found


def detect_soft_404(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    """Detect strong missing-resource templates served with a success status.

    This remains a hypothesis about search-engine treatment. Requiring both a
    missing-page heading and a missing-resource body avoids treating legitimate
    educational articles about HTTP errors as unavailable pages.
    """
    ctx = _context(context)
    intended = _normalised_intended_urls(ctx)
    found = []
    exact_headings = {
        "404", "404 not found", "not found", "page not found",
        "document not found", "resource not found",
    }
    body_pattern = re.compile(
        r"\b(?:we|the server)\s+(?:could not|couldn't|cannot|can't)\s+find\b"
        r"|\brequested\s+(?:page|document|resource)\b.{0,40}\b(?:not found|unavailable|does not exist)\b"
        r"|\b(?:page|document|resource)\s+(?:was|is)\s+not\s+found\b",
        flags=re.IGNORECASE,
    )
    for crawl in crawls:
        url = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        requested = normalise_url(crawl.url, ctx.site_url)
        if (crawl.status_code != 200 or not url or not crawl.main_content_observed
                or requested != url or crawl.indexability == "blocked" or (intended and url not in intended)):
            continue
        headings = {
            " ".join(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))
            for value in (crawl.title, crawl.main_heading) if value.strip()
        }
        if not (headings & exact_headings) or not body_pattern.search(crawl.main_text):
            continue
        found.append(_candidate("soft_404", url,
            "A strong missing-resource template was observed behind an HTTP 200 response; verify intended content and search-engine handling.",
            [{"source": "crawler", "claim_type": "INFERENCE", "status_code": 200,
              "heading_signals": sorted(headings & exact_headings), "main_content_observed": True,
              "owner_intends_indexable": url in intended if intended else None,
              "search_engine_soft_404_status": "unknown"}], ctx,
            confidence=0.75, risk=0.4,
            flags=["soft_404_is_hypothesis", "verify_rendered_response_and_owner_intent"],
            alternatives=["A deliberately styled error demonstration", "Temporary application fallback", "Search engine accepts the page as content"]))
    return found


def _content_shingles(text: str, width: int = 4) -> set[tuple[str, ...]]:
    tokens = re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
    return {tuple(tokens[index:index + width]) for index in range(max(0, len(tokens) - width + 1))}


def detect_topic_overlap(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    """Lexical overlap is an investigation hypothesis, not query cannibalisation.

    Fixed conservative thresholds use main content, excluding site navigation.
    A later independent intent/outcome review can legitimately decide NO-ACTION.
    """
    ctx = _context(context)
    intended = _normalised_intended_urls(ctx)
    pages = {}
    for crawl in crawls:
        url = normalise_url(crawl.final_url or crawl.url, ctx.site_url)
        requested = normalise_url(crawl.url, ctx.site_url)
        canonical = normalise_url(crawl.canonical or "", url) if crawl.canonical else url
        if (crawl.status_code == 200 and url and requested == url and canonical == url
                and crawl.indexability != "blocked" and crawl.main_content_observed
                and (not intended or url in intended) and len(crawl.main_text.split()) >= 80):
            pages[url] = crawl
    shingles = {url: _content_shingles(crawl.main_text) for url, crawl in pages.items()}
    stop = {"a", "an", "and", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the", "to", "with", "your"}
    headings = {url: query_tokens(crawl.main_heading or crawl.title.split("|")[0]) - stop for url, crawl in pages.items()}
    found = []
    urls = sorted(pages)
    for index, left in enumerate(urls):
        for right in urls[index + 1:]:
            common_heading = headings[left] & headings[right]
            union_heading = headings[left] | headings[right]
            heading_similarity = len(common_heading) / len(union_heading) if union_heading else 0
            union = shingles[left] | shingles[right]
            content_similarity = len(shingles[left] & shingles[right]) / len(union) if union else 0
            if len(common_heading) < 2 or heading_similarity < 0.5 or content_similarity < 0.35:
                continue
            found.append(_candidate("potential_topic_overlap", left,
                "Two pages have overlapping headings and main-text passages; verify whether they serve distinct useful needs.",
                [{"source": "crawler", "claim_type": "INFERENCE", "pages": [left, right],
                  "heading_jaccard": round(heading_similarity, 4), "main_fourgram_jaccard": round(content_similarity, 4),
                  "observed_query_cannibalisation": None, "matching_search_intent_verified": False}], ctx,
                confidence=0.5, risk=0.4, flags=["lexical_overlap_does_not_prove_cannibalisation", "no_merge_without_intent_and_outcome_evidence"],
                alternatives=["Distinct audiences using similar terminology", "Deliberate progressive exercises", "Useful alternative formats"]))
    return found


def detect_sitemap_inconsistencies(crawls: list[CrawlResult], context: AnalysisContext | dict[str, Any] | None = None) -> list[OpportunityCandidate]:
    """Compare an attested release inventory with fully retrieved sitemap URLs.

    Sitemaps are hints, not compulsory lists. Missing entries warrant review, not
    a claim of deindexing. A missing sitemap is unknown, not an empty complete one.
    """
    ctx = _context(context)
    if not ctx.inventory_complete or not ctx.sitemap_complete:
        return []
    inventory = {normalise_url(url, ctx.site_url) for url in ctx.inventory_urls} - {""}
    intended = {normalise_url(url, ctx.site_url) for url in ctx.intended_indexable_urls} - {""}
    sitemap = {normalise_url(url, ctx.site_url) for url in ctx.sitemap_urls} - {""}
    inventory = {url for url in inventory if _same_site(url, ctx.site_url)}
    intended = {url for url in intended if _same_site(url, ctx.site_url)}
    sitemap = {url for url in sitemap if _same_site(url, ctx.site_url)}
    observations = {normalise_url(crawl.url, ctx.site_url): crawl for crawl in crawls}
    found = []
    for url in sorted((inventory & intended) - sitemap):
        crawl = observations.get(url)
        if not crawl or crawl.status_code != 200 or crawl.indexability != "eligible":
            continue
        if crawl.canonical and normalise_url(crawl.canonical, url) != url:
            continue
        found.append(_candidate("sitemap_missing_page", url,
            "An owner-intended indexable release page is absent from the inspected sitemap; confirm whether submission would be useful.",
            [{"source": "crawler_inventory", "claim_type": "FACT", "listed_in_sitemap": False,
              "in_release_inventory": True, "status_code": 200, "actual_google_index_status": "unknown"}], ctx,
            confidence=0.75, flags=["sitemap_submission_is_optional", "absence_does_not_prove_indexing_problem"],
            alternatives=["Deliberate sitemap selection", "Adequate discovery through existing links"]))
    for url in sorted(sitemap):
        crawl = observations.get(url)
        if not crawl or crawl.status_code not in {404, 410}:
            continue
        outside_inventory = url not in inventory
        kind = "sitemap_unknown_page" if outside_inventory else "sitemap_unavailable_url"
        finding = ("The sitemap includes a URL outside the attested release inventory that returned a missing-page status."
                   if outside_inventory else
                   "The sitemap includes an inventoried URL that returned a terminal missing-page status.")
        found.append(_candidate(kind, url, finding,
            [{"source": "crawler_inventory", "claim_type": "FACT", "listed_in_sitemap": True,
              "in_release_inventory": not outside_inventory, "status_code": crawl.status_code,
              "owner_intends_indexable": url in intended if intended else None}], ctx,
            confidence=0.9, flags=["verify_deployment_consistency_before_change"],
            alternatives=["Sitemap and content deployed at different times", "Temporary routing failure"]))
    return found


def compare_serps(before: list[str] | list[dict[str, Any]], after: list[str] | list[dict[str, Any]]) -> dict[str, Any]:
    """Compare ordered URL observations; provider/context comparability is caller-owned."""
    def extract(items: list[Any]) -> list[str]:
        return list(dict.fromkeys(normalise_url(item if isinstance(item, str) else item.get("url", "")) for item in items))
    old, new = [url for url in extract(before) if url], [url for url in extract(after) if url]
    union = set(old) | set(new)
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "jaccard_overlap": len(set(old) & set(new)) / len(union) if union else None,
            "position_changes": [{"url": url, "before": old.index(url) + 1, "after": new.index(url) + 1,
                                  "change": old.index(url) - new.index(url)} for url in old if url in new],
            "claim_type": "FACT", "causal_attribution": None,
            "quality_flags": ["verify_same_query_country_device_locale_and_result_type", "serp_is_a_time_and_context_sample"]}


def compare_page_versions(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Inspectable structured diff; no HTML is executed or treated as policy."""
    keys = sorted(before.keys() | after.keys())
    changes = {key: {"before": before.get(key), "after": after.get(key), "before_present": key in before,
                     "after_present": key in after} for key in keys if before.get(key) != after.get(key) or (key in before) != (key in after)}
    text_diff = "".join(difflib.unified_diff(str(before.get("content", "")).splitlines(keepends=True),
                    str(after.get("content", "")).splitlines(keepends=True), fromfile="before", tofile="after"))
    return {"changed_fields": list(changes), "changes": changes, "content_diff": text_diff,
            "source_trust": "untrusted_external", "instructions_executed": False}


def analyze(
    gsc_rows: list[GSCRow],
    ga4_rows: list[GA4Row],
    crawls: list[CrawlResult],
    context: AnalysisContext | dict[str, Any] | None = None,
) -> list[OpportunityCandidate]:
    """Rank evidence-backed investigation candidates; this API cannot mutate a site."""
    ctx = _context(context)
    gsc_rows = _valid_gsc(gsc_rows)
    candidates = (
        detect_high_impression_positions(gsc_rows, ctx)
        + detect_ctr_anomaly(gsc_rows, context=ctx)
        + detect_content_decay(gsc_rows, ctx)
        + detect_cannibalisation(gsc_rows, ctx)
        + detect_orphan_pages(crawls, context=ctx)
        + detect_broken_links(crawls, ctx)
        + detect_redirect_chains(crawls, ctx)
        + detect_duplicate_metadata(crawls, ctx)
        + detect_indexability(crawls, ctx)
        + detect_weak_internal_links(crawls, ctx)
        + detect_canonical_cycles(crawls, ctx)
        + detect_canonical_mismatch(crawls, ctx)
        + detect_thin_content(crawls, ctx)
        + detect_soft_404(crawls, ctx)
        + detect_topic_overlap(crawls, ctx)
        + detect_sitemap_inconsistencies(crawls, ctx)
    )
    ga4_by_page: dict[str, list[GA4Row]] = defaultdict(list)
    for row in ga4_rows:
        if row.channel == "Organic Search":
            ga4_by_page[normalise_url(row.landing_page, ctx.site_url)].append(row)
    # Enrich with measured outcomes without replacing unconfigured qualification
    # with GA4 key-event counts, or labelling a missing row as zero conversions.
    for candidate in candidates:
        page_rows = ga4_by_page.get(normalise_url(candidate.page_url, ctx.site_url), [])
        qualified = (sum(row.qualified_conversions for row in page_rows if row.qualified_conversions is not None)
                     if page_rows and all(row.qualified_conversions is not None for row in page_rows) else None)
        value = (sum(row.conversion_value for row in page_rows if row.conversion_value is not None)
                 if ctx.conversion_value_mapping_verified and page_rows and all(row.conversion_value is not None for row in page_rows) else None)
        candidate.evidence.append({"source": "ga4", "claim_type": "FACT", "organic_sessions": sum(row.sessions for row in page_rows) if page_rows else None,
            "qualified_conversions": qualified, "qualified_conversion_value": value,
            "dates": sorted({row.date.isoformat() for row in page_rows}), "estimated_incremental_value": None})
        candidate.quality_flags = sorted(set(candidate.quality_flags + [flag for row in page_rows for flag in row.quality_flags]
                                             + ([] if page_rows else ["ga4_page_observations_unavailable"])))
    return sorted(candidates, key=lambda item: (-item.score, item.kind, item.page_url, str(item.evidence)))
