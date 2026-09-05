"""Evaluator-only legacy Test Lab scoring.

This module deliberately lives outside the runtime image. It may read private
ground-truth labels only after the runtime service has durably frozen an
observation/decision packet.
"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config.settings import Settings
from backend.app.contracts import ActionKind, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.guardrails.policy import classify_risk
from backend.app.services.test_lab import TARGET_KIND, _path, freeze_decisions


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
            "private_truth_commitment_recorded_in_evaluator_evidence": True, "is_fixture": fixture,
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
