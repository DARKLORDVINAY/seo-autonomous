"""Independent regressions from adversarial review; no browser or network access."""
from __future__ import annotations

import hashlib
import json

import pytest

from backend.app.contracts import ActionKind
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.guardrails.policy import classify_risk
from backend.app.services.control import create_site
from benchmarks.legacy_lab_evaluator import evaluate_frozen_packet
from scripts.lab_rollback_drill import run_drill


def test_rollback_rejects_symlink_parent_that_points_inside_original_release(tmp_path, monkeypatch):
    release = tmp_path / "release"
    page = release / "guides/example/index.html"
    page.parent.mkdir(parents=True)
    before = b"<!doctype html><title>Example</title>"
    page.write_bytes(before)
    (release / "inventory.json").write_text(json.dumps({
        "pages": [{"path": "/guides/example/", "content_sha256": hashlib.sha256(before).hexdigest()}],
    }))
    alias = tmp_path / "outside-alias"
    alias.symlink_to(release, target_is_directory=True)

    def forbidden_copy(*args, **kwargs):
        pytest.fail("A destination inside the original release must be rejected before copytree")

    monkeypatch.setattr("scripts.lab_rollback_drill.shutil.copytree", forbidden_copy)
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            site = create_site(session, name="Path boundary fixture", base_url="https://example.test", fixture=True)
            with pytest.raises(ValueError, match="outside"):
                run_drill(session, site.id, release, alias / "nested-copy", "/guides/example/")
        assert page.read_bytes() == before
        assert not (release / "nested-copy").exists()
    finally:
        engine.dispose()


def independent_packet():
    """A single independently specified issue; no public lab answer key is read."""
    packet = {
        "candidates": [{"candidate_id": "observed-orphan", "kind": "orphan_page", "path": "/orphan/",
                        "related_paths": ["/orphan/"]}],
        "page_decisions": [{"path": "/orphan/", "decision": "INVESTIGATE"},
                           {"path": "/clean/", "decision": "NO-ACTION"}],
        "context": {"inventory_complete": True, "crawl_coverage_complete": True},
        "crawl_results": [{"url": "https://example.test/orphan/", "status_code": 200, "links": []},
                          {"url": "https://example.test/clean/", "status_code": 200, "links": []}],
        "high_critical_policy_probes": [
            {"action_kind": kind.value, "allowed": False} for kind in ActionKind
            if classify_risk(kind).value in {"HIGH", "CRITICAL"}
        ],
        "external_mutation_events": [],
        "cycle": {"result": {"execution": {"status": "shadow"}}},
    }
    truth = {
        "expected_issues": [{"id": "orphan", "kind": "orphan_page", "path": "/orphan/",
                             "evidence_facets": {"incoming_html_sources": 0}}],
        "clean_control_pages": ["/clean/"],
    }
    return packet, truth


def test_benchmark_cannot_pass_with_a_known_false_no_action():
    packet, truth = independent_packet()
    assert evaluate_frozen_packet(packet, truth)["structural_benchmark_passed"]
    packet["page_decisions"][0]["decision"] = "NO-ACTION"
    result = evaluate_frozen_packet(packet, truth)
    assert result["true_positives"] == 1 and result["false_no_action"] == 1
    assert not result["structural_benchmark_passed"]


@pytest.mark.parametrize("remaining_probes", [0, 1])
def test_benchmark_requires_observed_complete_high_critical_probe_set(remaining_probes):
    packet, truth = independent_packet()
    assert evaluate_frozen_packet(packet, truth)["structural_benchmark_passed"]
    packet["high_critical_policy_probes"] = packet["high_critical_policy_probes"][:remaining_probes]
    result = evaluate_frozen_packet(packet, truth)
    assert not result["high_critical_intercepted"]
    assert not result["structural_benchmark_passed"]


def overlap_packet():
    """Use an independent invented pair rather than the release's answer key."""
    packet, truth = independent_packet()
    text = (
        "Choose a short document and describe the question its title promises to answer. "
        "Read each paragraph carefully before deciding whether that promise is fulfilled. "
        "Keep a copy of the original wording so your observations remain separate from your proposed edits. "
        "A useful review explains the reader benefit and the smallest change needed to provide it. "
        "Record uncertainties and ask for missing evidence whenever the document cannot support a factual claim. "
        "Leaving clear wording unchanged is a legitimate outcome of this exercise."
    )
    packet["candidates"] = [{"candidate_id": "observed-pair", "kind": "potential_topic_overlap",
                             "path": "/left/", "related_paths": ["/left/", "/right/"]}]
    packet["page_decisions"] = [{"path": "/left/", "decision": "INVESTIGATE"},
                                {"path": "/right/", "decision": "INVESTIGATE"}]
    packet["crawl_results"] = [
        {"url": "https://example.test/left/", "status_code": 200, "main_heading": "Practice a document title review",
         "main_text": text, "title": "Document title review", "meta_description": "Practice describing a document."},
        {"url": "https://example.test/right/", "status_code": 200, "main_heading": "Practice a document title audit",
         "main_text": text + " Compare your audit notes after finishing.", "title": "Document title audit",
         "meta_description": "Practice auditing a document."},
    ]
    truth = {"expected_issues": [{"id": "pair", "kind": "potential_topic_overlap", "path": "/left/",
              "related_paths": ["/right/"], "evidence_facets": {
                  "same_task_and_near_duplicate_main_text": True, "metadata_distinct": True,
                  "actual_query_cannibalisation": "unknown_without_search_console_data"}}]}
    return packet, truth


def test_overlap_label_and_paths_cannot_replace_observed_text_evidence():
    packet, truth = overlap_packet()
    assert evaluate_frozen_packet(packet, truth)["structural_benchmark_passed"]
    packet["crawl_results"][0].update(main_heading="Physics", main_text="An original physics tutorial.")
    packet["crawl_results"][1].update(main_heading="Cooking", main_text="An unrelated cooking checklist.")
    result = evaluate_frozen_packet(packet, truth)
    assert (result["true_positives"], result["false_positives"], result["false_negatives"]) == (0, 1, 1)
    assert not result["structural_benchmark_passed"]
    assert result["seed_evidence_disagreements"][0]["failed"][0]["facet"] == "same_task_and_near_duplicate_main_text"


@pytest.mark.parametrize("field", ["title", "meta_description"])
def test_overlap_seed_requires_its_distinct_metadata_premise(field):
    packet, truth = overlap_packet()
    packet["crawl_results"][1][field] = packet["crawl_results"][0][field]
    result = evaluate_frozen_packet(packet, truth)
    assert not result["structural_benchmark_passed"]
    assert result["seed_evidence_disagreements"][0]["failed"][0]["facet"] == "metadata_distinct"


@pytest.mark.parametrize("kind", ["duplicate_title", "duplicate_meta_description", "potential_topic_overlap"])
def test_no_action_on_a_second_affected_pair_page_cannot_pass(kind):
    packet, truth = overlap_packet()
    packet["candidates"][0]["kind"] = truth["expected_issues"][0]["kind"] = kind
    # This case isolates page-decision consistency, separately from text checks.
    truth["expected_issues"][0]["evidence_facets"] = {}
    assert evaluate_frozen_packet(packet, truth)["structural_benchmark_passed"]
    packet["page_decisions"][1]["decision"] = "NO-ACTION"
    result = evaluate_frozen_packet(packet, truth)
    assert result["true_positives"] == 1 and result["false_no_action"] == 1
    assert not result["structural_benchmark_passed"]


@pytest.mark.parametrize("kind", ["canonical_mismatch", "broken_internal_link", "weak_internal_links"])
def test_related_evidence_page_can_remain_no_action(kind):
    packet, truth = overlap_packet()
    packet["candidates"][0]["kind"] = truth["expected_issues"][0]["kind"] = kind
    truth["expected_issues"][0]["evidence_facets"] = {}
    packet["page_decisions"][1]["decision"] = "NO-ACTION"
    truth["clean_control_pages"] = ["/right/"]
    result = evaluate_frozen_packet(packet, truth)
    assert result["false_no_action"] == 0 and result["correct_no_action"] == 1
    assert result["structural_benchmark_passed"]
