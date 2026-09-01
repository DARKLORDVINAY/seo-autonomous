"""Interpret structured observation coverage; never interpret webpage prose as policy."""
from __future__ import annotations

import re
from typing import Any

# Incomplete search-query population is expected even for final, fully paginated
# GSC responses. It must not be conflated with incomplete time periods/outages.
_GSC_POPULATION_CAVEATS = {
    "gsc_top_rows_only", "anonymised_queries_omitted_do_not_sum_as_page_totals",
}
_UNSAFE_FLAGS = {
    "partial", "partial_data", "partial_period", "partial_gsc_data", "unknown_data_state",
    "missing_dates", "tracking_outage", "tracking_error", "tracking_errors", "tracking_unverified",
    "tracking_unhealthy", "measurement_error", "measurement_outage", "conversion_decline",
    "conversion_regression", "api_disagreement", "row_budget_reached", "pagination_incomplete",
    "privacy_thresholding", "subject_to_thresholding", "thresholding", "sampling", "sampling_metadatas",
    "data_loss_from_other_row", "business_conversion_definition_unconfirmed",
    "qualified_conversion_definition_unconfirmed", "unqualified_conversions", "landing_page_not_set",
    "data_not_final", "report_timezone_unconfirmed", "timezone_disagreement", "sampled_report", "aggregated_other_rows",
    "qualified_outcome_missing", "schema_restrictions", "empty_report_reason",
}
_TRUE_MEANS_UNSAFE = {
    "partial_data", "tracking_outage", "tracking_error", "measurement_error", "conversion_decline",
    "conversion_regression", "api_disagreement", "subject_to_thresholding", "data_loss_from_other_row",
    "sampling_metadatas", "missing_dates", "privacy_thresholding",
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()).strip("_")


def quality_reasons(content: Any) -> list[str]:
    if not isinstance(content, dict):
        return ["evidence_must_be_structured"]
    reasons: set[str] = set()
    flags: set[str] = set()
    pending: list[tuple[Any, int]] = [(content, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 16 or count > 500_000:
            reasons.add("evidence_shape_exceeds_validation_budget")
            break
        if isinstance(node, list):
            pending.extend((value, depth + 1) for value in node)
        elif isinstance(node, dict):
            for name, value in node.items():
                normalized = _key(name)
                if normalized == "data_state" and value != "final":
                    reasons.add("partial_or_unknown_evidence_cannot_authorise_write")
                if normalized in _TRUE_MEANS_UNSAFE and value:
                    reasons.add("unsafe_observation_quality:" + normalized)
                if normalized == "qualified_conversion_semantics_verified" and value is not True:
                    reasons.add("qualified_conversion_semantics_unverified")
                if normalized == "quality_flags":
                    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                        reasons.add("malformed_evidence_quality_flags")
                    else:
                        flags.update(_key(item) for item in value)
                # Strings such as crawl text are inert and are never parsed as
                # JSON or searched for apparent system instructions.
                if isinstance(value, (dict, list)):
                    pending.append((value, depth + 1))
    for flag in flags & _UNSAFE_FLAGS:
        reasons.add("unsafe_observation_quality:" + flag)
    if content.get("complete") is False:
        metadata = content.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        documented_gsc_population = (
            bool(flags & _GSC_POPULATION_CAVEATS)
            and metadata.get("data_state") == "final"
            and metadata.get("pagination_exhausted") is True
            and metadata.get("missing_dates") == []
        )
        documented_ga4_population = (
            metadata.get("qualified_conversion_semantics_verified") is True
            and metadata.get("pagination_exhausted") is True
            and metadata.get("missing_dates") == []
        )
        if not documented_gsc_population and not documented_ga4_population:
            reasons.add("incomplete_evidence_without_qualified_coverage")
    return sorted(reasons)
