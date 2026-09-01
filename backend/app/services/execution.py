"""Capability-scoped execution with durable intent, leases, and reconciliation.

Each public service owns its commits. The HTTP/MCP boundary must authenticate
approval calls with the separate approval capability; model/tool tokens must
never call approve_revision. A CMS write is made only after intent and a durable,
non-expiring page lease have committed. Uncertain writes are never retried.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.contracts import ActionKind, CMSPage, CMSProvider, ConcurrencyConflict, VerificationPacket, stable_hash, utcnow
from backend.app.db.models import (
    Action, ActionEvent, Approval, Evidence, ExecutionLease, Experiment, FailureCase, Guardrail, Page,
    PageVersion, Revision, RollbackEvent, Site, Verification,
)
from backend.app.guardrails.evidence import quality_reasons
from backend.app.guardrails.policy import LOCAL_KINDS, classify_risk, evaluate_policy, validate_revision

REQUIRED_CHECKS = frozenset({
    "factual_accuracy", "policy_compliance", "conversion_guard", "source_independence",
    "alternatives_considered", "tracking_quality",
})
_TRUSTED_SOURCE_TYPES = frozenset({"gsc", "ga4", "crawl", "serp", "ai_search", "cms", "business", "brand_facts", "human_observation", "fixture"})
_FINAL_EVENTS = frozenset({"succeeded", "blocked", "failed", "reconciliation_required", "local_draft_created"})
PREDICTION_OWNER = "execution-engine"
PREDICTION_SOURCE = "canonical:experiment-prediction:v1"
PREDICTION_SEMANTICS = "probability_of_primary_outcome_success"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _snapshot(page: Page) -> CMSPage:
    metadata = page.metadata_json or {}
    if metadata.get("cms_snapshot"):
        return CMSPage.model_validate(metadata["cms_snapshot"])
    return CMSPage(
        external_id=page.external_id or "", url=page.url, title=page.title or "",
        content=page.content_html or "", meta_description=page.meta_description or "",
        status=metadata.get("cms_status", "publish"), slug=metadata.get("cms_slug", ""),
        modified_gmt=metadata.get("cms_modified_gmt", ""), metadata=metadata.get("cms_metadata", {}),
    )


def _changes(before: CMSPage, after: CMSPage) -> dict[str, Any]:
    old = before.model_dump(exclude={"modified_gmt"})
    new = after.model_dump(exclude={"modified_gmt"})
    return {key: new[key] for key in old if old[key] != new[key]}


def _revision_digest(revision: Revision) -> str:
    return stable_hash({
        "site_id": revision.site_id, "page_id": revision.page_id, "kind": revision.kind,
        "before": revision.before_json, "after": revision.after_json,
        "before_hash": revision.before_hash, "changes": revision.changes_json,
        "evidence_ids": sorted(revision.evidence_ids_json or []), "reason": revision.reason,
        "created_by": revision.created_by, "experiment_id": revision.experiment_id,
    })


def prediction_specification(experiment: Experiment, site: Site, revision: Revision) -> dict[str, Any]:
    """The prespecified estimand/configuration, separate from forecast confidence.

    Returning JSON values also detaches mutable ORM dictionaries from the saved
    evidence. Forecast identity/probability are read only from frozen evidence
    after deployment; editing their display fields cannot rewrite a prediction.
    """
    from copy import deepcopy

    return deepcopy({
        "site_id": site.id, "experiment_id": experiment.id, "page_id": experiment.page_id,
        "page_url": revision.before_json.get("url"), "hypothesis": experiment.hypothesis,
        "mechanism": experiment.mechanism, "primary_outcome": experiment.primary_outcome,
        "secondary_outcomes": experiment.secondary_outcomes_json or [],
        "baseline_start": experiment.baseline_start.isoformat() if experiment.baseline_start else None,
        "baseline_end": experiment.baseline_end.isoformat() if experiment.baseline_end else None,
        "control_pages": experiment.control_pages_json or [],
        "evaluation_windows": experiment.evaluation_windows_json or [],
        "measurement_config": (experiment.analysis_json or {}).get("measurement_config", {}),
        "conversion_definition_hash": stable_hash(site.conversion_definition),
    })


def _freeze_prediction(session: Session, site: Site, revision: Revision, action: Action, *, is_fixture: bool) -> Evidence:
    """Persist with the dispatch lease, before any write; never infer a forecast.

    The dispatch event is the authority for locating this record. No mutable
    experiment pointer or client-supplied Evidence ID can substitute for it.
    """
    experiment = session.scalar(select(Experiment).where(
        Experiment.id == revision.experiment_id, Experiment.site_id == site.id,
    ).with_for_update().execution_options(populate_existing=True))
    analysis = experiment.analysis_json or {}
    supplied = experiment.predicted_confidence
    valid_probability = (isinstance(supplied, (int, float)) and not isinstance(supplied, bool)
                         and math.isfinite(supplied) and 0 <= supplied <= 1)
    effect = experiment.predicted_effect
    valid_effect = effect is None or (isinstance(effect, (int, float)) and not isinstance(effect, bool) and math.isfinite(effect))
    semantics = analysis.get("prediction_semantics")
    criterion = analysis.get("success_criterion")
    reasons = []
    if not valid_probability:
        reasons.append("probability_of_success_not_supplied")
    if semantics != PREDICTION_SEMANTICS:
        reasons.append("probability_semantics_not_explicit")
    if not isinstance(criterion, str) or not criterion.strip():
        reasons.append("success_criterion_not_prespecified")
    if not valid_effect:
        reasons.append("predicted_effect_not_finite")
    if experiment.deployed_at is not None:
        reasons.append("experiment_already_deployed")
    if not experiment.baseline_start or not experiment.baseline_end:
        reasons.append("baseline_not_prespecified")
    # The proposing principal owns this forecast. A JSON field cannot falsely
    # assign its successes/failures to a different model or a human reviewer.
    agent_id = revision.created_by
    if analysis.get("agent_id") not in (None, agent_id):
        reasons.append("forecast_agent_does_not_match_proposer")
    frozen_at = utcnow()
    content = {
        "version": 1, "site_id": site.id, "experiment_id": experiment.id, "action_id": action.id,
        "revision_id": revision.id, "revision_hash": revision.revision_hash,
        "agent_id": agent_id, "action_category": revision.kind, "frozen_at": frozen_at.isoformat(),
        "prediction_status": "PRESPECIFIED" if not reasons else "UNKNOWN",
        "probability_of_success": float(supplied) if not reasons else None,
        "supplied_confidence": float(supplied) if valid_probability else None,
        "confidence_semantics": semantics if isinstance(semantics, str) else None,
        "success_criterion": criterion if isinstance(criterion, str) else None,
        "predicted_effect": effect if valid_effect else None,
        "exclusion_reasons": reasons, "specification": prediction_specification(experiment, site, revision),
    }
    evidence = Evidence(site_id=site.id, source=PREDICTION_SOURCE, source_type="experiment_prediction",
        content=content, observed_at=frozen_at, content_hash=stable_hash(content), owner=PREDICTION_OWNER,
        # Certainty that the snapshot was captured is not confidence in success.
        confidence=1.0, is_fixture=is_fixture)
    session.add(evidence)
    session.flush()
    return evidence


def _event(session: Session, action: Action, event_type: str, details: dict[str, Any] | None = None) -> None:
    session.add(ActionEvent(site_id=action.site_id, action_id=action.id, event_type=event_type, details_json=details or {}))
    if event_type == "blocked":
        session.add(Guardrail(site_id=action.site_id, action_id=action.id, rule="execution_policy",
                              outcome="BLOCK", reason="; ".join((details or {}).get("reasons", ["guardrail denied"])),
                              context_json={"risk": action.risk, "kind": action.kind}))
    if event_type in {"failed", "reconciliation_required"}:
        session.add(FailureCase(
            site_id=action.site_id, action_id=action.id, category="execution_failure",
            predicted="A bounded CMS action completes and reads back the approved state",
            actual=event_type, root_cause=(details or {}).get("error_type", "remote verification mismatch"),
            agent_responsible=action.actor, detection_method="CMS response, fingerprint verification, or storage exception",
            preventative_change="Retain page lease until operator reconciliation; never blindly retry",
            details_json=details or {},
        ))
    if event_type in {"succeeded", "blocked", "failed", "reconciliation_required"} and action.revision_id:
        revision = session.get(Revision, action.revision_id)
        original_id = (revision.changes_json or {}).get("__rollback_of") if revision else None
        if original_id and action.kind == revision.kind:
            session.add(RollbackEvent(site_id=action.site_id, action_id=original_id,
                                      rollback_action_id=action.id, reason=action.reason, actor=action.actor,
                                      status=event_type, details_json=details or {}))


def _events(session: Session, action_id: str) -> list[ActionEvent]:
    return list(session.scalars(select(ActionEvent).where(ActionEvent.action_id == action_id).order_by(ActionEvent.created_at, ActionEvent.id)))


def _result(session: Session, action: Action, *, replay: bool = False) -> dict[str, Any]:
    events = _events(session, action.id)
    terminal = [event for event in events if event.event_type in _FINAL_EVENTS]
    event = terminal[-1] if terminal else (events[-1] if events else None)
    return {
        "action_id": action.id, "revision_id": action.revision_id, "risk": action.risk,
        "status": event.event_type if event else "requested", "idempotent_replay": replay,
        "details": event.details_json if event else {},
    }


def _new_action(
    session: Session, *, site_id: str, revision: Revision | None, kind: str,
    actor: str, reason: str, key: str | None = None, payload: dict[str, Any] | None = None,
) -> Action:
    action = Action(
        id=str(uuid4()), site_id=site_id, revision_id=revision.id if revision else None,
        kind=kind, risk=classify_risk(revision.kind if revision else kind).value,
        actor=actor, reason=reason, experiment_id=revision.experiment_id if revision else None,
        idempotency_key=key or str(uuid4()), payload_json=payload or {},
    )
    session.add(action)
    session.flush()
    return action


def _audit(
    session: Session, *, site_id: str, revision: Revision | None, kind: str, actor: str,
    status: str, details: dict[str, Any], reason: str = "control-plane mutation",
) -> dict[str, Any]:
    action = _new_action(session, site_id=site_id, revision=revision, kind=kind, actor=actor, reason=reason)
    _event(session, action, status, details)
    session.commit()
    return _result(session, action)


def propose_revision(
    session: Session, *, site_id: str, page_id: str, kind: ActionKind | str, after: CMSPage,
    created_by: str, reason: str, evidence_ids: list[str], experiment_id: str,
) -> dict[str, Any]:
    site = session.get(Site, site_id)
    page = session.get(Page, page_id)
    if site is None:
        raise ValueError("unknown_site")
    if page is None or page.site_id != site_id:
        return _audit(session, site_id=site_id, revision=None, kind=str(kind), actor=created_by,
                      status="blocked", details={"reasons": ["page_not_in_site"]})
    before = _snapshot(page)
    gate = validate_revision(kind, before, after, base_url=site.base_url)
    reasons = list(gate.reasons)
    if site.autonomy_level == 0:
        reasons.append("observer_mode_cannot_create_revisions")
    if not created_by.strip() or not reason.strip() or len(reason) > 10_000:
        reasons.append("actor_and_bounded_reason_required")
    experiment = session.get(Experiment, experiment_id)
    if experiment is None or experiment.site_id != site_id or experiment.page_id not in (None, page_id):
        reasons.append("experiment_not_in_site_or_page")
    if not evidence_ids:
        reasons.append("supporting_evidence_required")
    for evidence_id in set(evidence_ids):
        evidence = session.get(Evidence, evidence_id)
        if evidence is None or evidence.site_id != site_id:
            reasons.append("evidence_not_in_site")
    if reasons:
        return _audit(session, site_id=site_id, revision=None, kind=str(kind), actor=created_by,
                      status="blocked", details={"reasons": sorted(set(reasons))})
    revision = Revision(
        id=str(uuid4()), site_id=site_id, page_id=page_id, kind=str(kind),
        changes_json=_changes(before, after), before_json=before.model_dump(mode="json"),
        after_json=after.model_dump(mode="json"), before_hash=before.fingerprint,
        revision_hash="", evidence_ids_json=sorted(set(evidence_ids)), reason=reason,
        created_by=created_by, experiment_id=experiment_id,
    )
    revision.revision_hash = _revision_digest(revision)
    session.add(revision)
    session.flush()
    result = _audit(session, site_id=site_id, revision=revision, kind="propose_revision", actor=created_by,
                    status="local_draft_created", reason=reason,
                    details={"revision_hash": revision.revision_hash, "classification": gate.risk.value})
    result["revision_hash"] = revision.revision_hash
    return result


def record_verification(session: Session, *, revision_id: str, packet: VerificationPacket) -> dict[str, Any]:
    revision = session.get(Revision, revision_id)
    if revision is None:
        raise ValueError("unknown_revision")
    # All verdicts, including hostile or mistaken PASS packets, remain available
    # for review. Authority is re-derived from identities and evidence at dispatch.
    session.scalar(select(Site).where(Site.id == revision.site_id).with_for_update())
    verification = Verification(
        id=str(uuid4()), site_id=revision.site_id, revision_id=revision.id,
        revision_hash=revision.revision_hash, verifier_id=packet.verifier_id,
        verdict=packet.verdict, independent=packet.independent, confidence=packet.confidence,
        action_safe=packet.action_safe, packet_json=packet.model_dump(mode="json"),
    )
    session.add(verification)
    session.flush()
    result = _audit(session, site_id=revision.site_id, revision=revision, kind="record_verification",
                    actor=packet.verifier_id, status="local_draft_created",
                    details={"verification_id": verification.id, "verdict": packet.verdict})
    result["verification_id"] = verification.id
    return result


def approve_revision(
    session: Session, *, revision_id: str, approved_by: str, reason: str,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """Trusted service only. HTTP routes must enforce the separate human token."""
    revision = session.get(Revision, revision_id)
    if revision is None:
        raise ValueError("unknown_revision")
    site = session.scalar(select(Site).where(Site.id == revision.site_id).with_for_update())
    expiry = _as_utc(expires_at) if expires_at else utcnow() + timedelta(hours=24)
    reasons: list[str] = []
    if classify_risk(revision.kind).value in {"HIGH", "CRITICAL"}:
        reasons.append("high_and_critical_actions_not_implemented")
    if not approved_by.strip() or not reason.strip():
        reasons.append("identified_approver_and_reason_required")
    if expiry <= utcnow() or expiry > utcnow() + timedelta(days=7):
        reasons.append("approval_expiry_must_be_within_seven_days")
    if site is None or site.autonomy_level == 0:
        reasons.append("observer_mode_cannot_approve")
    if _revision_digest(revision) != revision.revision_hash:
        reasons.append("revision_integrity_failure")
    if reasons:
        return _audit(session, site_id=revision.site_id, revision=revision, kind="approve_revision", actor=approved_by,
                      status="blocked", details={"reasons": reasons})
    approval = Approval(
        id=str(uuid4()), site_id=revision.site_id, revision_id=revision.id,
        revision_hash=revision.revision_hash, approved_by=approved_by, decision="APPROVE",
        reason=reason, expires_at=expiry,
    )
    session.add(approval)
    session.flush()
    result = _audit(session, site_id=revision.site_id, revision=revision, kind="approve_revision", actor=approved_by,
                    status="local_draft_created", details={"approval_id": approval.id, "expires_at": expiry.isoformat()})
    result["approval_id"] = approval.id
    return result


def _evidence_reasons(session: Session, revision: Revision, *, is_fixture: bool, config: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    ids = revision.evidence_ids_json or []
    trusted_owners = set(config.get("trusted_evidence_owners", ["ingestion", "crawler", "business-owner", "fixture", "system"]))
    if not ids:
        reasons.append("supporting_evidence_required")
    for evidence_id in ids:
        evidence = session.get(Evidence, evidence_id)
        if evidence is None or evidence.site_id != revision.site_id:
            reasons.append("evidence_not_in_site")
            continue
        if evidence.source_type not in _TRUSTED_SOURCE_TYPES or evidence.owner not in trusted_owners:
            reasons.append("untrusted_evidence_provenance")
        if not evidence.source or evidence.status != "active" or evidence.confidence < 0.5:
            reasons.append("inactive_or_inadequate_evidence")
        if evidence.content_hash and stable_hash(evidence.content) != evidence.content_hash:
            reasons.append("evidence_integrity_failure")
        if not is_fixture and (evidence.is_fixture or evidence.source_type == "fixture"):
            reasons.append("fixture_evidence_cannot_authorise_production")
        if evidence.observed_at and _as_utc(evidence.observed_at) > utcnow() + timedelta(minutes=5):
            reasons.append("future_dated_evidence")
        reasons.extend(quality_reasons(evidence.content))
    return sorted(set(reasons))


def _verification_reasons(session: Session, revision: Revision, config: dict[str, Any]) -> list[str]:
    verification = session.scalar(select(Verification).where(Verification.revision_id == revision.id).order_by(Verification.created_at.desc(), Verification.id.desc()).limit(1))
    if verification is None:
        return ["independent_verification_required"]
    reasons: list[str] = []
    if verification.site_id != revision.site_id or verification.revision_hash != revision.revision_hash:
        reasons.append("verification_revision_binding_failed")
    if verification.verifier_id == revision.created_by or verification.verifier_id not in config.get("trusted_verifier_ids", []):
        reasons.append("verifier_not_independently_authorised")
    if not verification.independent or verification.verdict != "PASS" or not verification.action_safe:
        reasons.append("verifier_did_not_pass_action")
    if verification.confidence < max(0.7, float(config.get("min_verifier_confidence", 0.7))):
        reasons.append("verifier_confidence_below_threshold")
    packet = verification.packet_json or {}
    if not REQUIRED_CHECKS.issubset({key for key, value in packet.get("checks", {}).items() if value is True}):
        reasons.append("verifier_checks_incomplete")
    ids = packet.get("evidence_ids", [])
    if not ids or not set(ids).issubset(set(revision.evidence_ids_json or [])):
        reasons.append("verifier_evidence_not_bound_to_revision")
    if not packet.get("reasons") or not packet.get("alternative_explanations"):
        reasons.append("verifier_must_record_reason_and_alternatives")
    return reasons


def _approval_valid(session: Session, revision: Revision) -> bool:
    approval = session.scalar(select(Approval).where(Approval.revision_id == revision.id).order_by(Approval.created_at.desc(), Approval.id.desc()).limit(1))
    return bool(approval and approval.site_id == revision.site_id and approval.revision_hash == revision.revision_hash
                and approval.decision == "APPROVE" and approval.expires_at and _as_utc(approval.expires_at) > utcnow())


def _human_veto_active(session: Session, revision: Revision) -> bool:
    # Expiry limits a permission; it never turns an explicit refusal into
    # permission. Only a later explicit approval supersedes a human veto.
    approval = session.scalar(select(Approval).where(Approval.revision_id == revision.id).order_by(Approval.created_at.desc(), Approval.id.desc()).limit(1))
    return bool(approval and approval.site_id == revision.site_id and approval.revision_hash == revision.revision_hash
                and approval.decision in {"REJECT", "REVOKE"})


def _revision_reasons(session: Session, revision: Revision, site: Site) -> list[str]:
    reasons: list[str] = []
    try:
        before = CMSPage.model_validate(revision.before_json)
        after = CMSPage.model_validate(revision.after_json)
    except (ValueError, TypeError):
        return ["invalid_revision_snapshot"]
    if revision.before_hash != before.fingerprint or revision.revision_hash != _revision_digest(revision):
        reasons.append("revision_integrity_failure")
    actual_changes = _changes(before, after)
    stored_changes = {key: value for key, value in revision.changes_json.items() if key != "__rollback_of"}
    if actual_changes != stored_changes:
        reasons.append("revision_change_binding_failed")
    rollback_of = revision.changes_json.get("__rollback_of")
    if rollback_of:
        original_action = session.get(Action, rollback_of)
        original_revision = session.get(Revision, original_action.revision_id) if original_action else None
        succeeded = [e for e in _events(session, rollback_of) if e.event_type == "succeeded"] if original_action else []
        if not original_revision or original_action.site_id != site.id or original_revision.page_id != revision.page_id or not succeeded:
            reasons.append("rollback_original_action_invalid")
        elif (before.fingerprint != CMSPage.model_validate(succeeded[-1].details_json["after"]).fingerprint
              or after.fingerprint != CMSPage.model_validate(original_revision.before_json).fingerprint
              or revision.kind != original_revision.kind):
            reasons.append("rollback_must_exactly_reverse_original_action")
        else:
            reasons.extend(validate_revision(revision.kind, after, before, base_url=site.base_url).reasons)
    else:
        reasons.extend(validate_revision(revision.kind, before, after, base_url=site.base_url).reasons)
    return reasons


def _policy_reasons(session: Session, revision: Revision, site: Site, *, is_fixture: bool, production_enabled: bool,
                    max_autonomy_level: int | None = None) -> list[str]:
    config = site.config_json or {}
    reasons = _revision_reasons(session, revision, site)
    if _human_veto_active(session, revision):
        reasons.append("explicit_human_veto_blocks_all_autonomy")
    evidence_reasons = _evidence_reasons(session, revision, is_fixture=is_fixture, config=config)
    verification_reasons = _verification_reasons(session, revision, config)
    experiment = session.get(Experiment, revision.experiment_id) if revision.experiment_id else None
    valid_experiment = bool(experiment and experiment.site_id == site.id and experiment.page_id in (None, revision.page_id)
                            and experiment.hypothesis and experiment.primary_outcome)
    if revision.kind not in LOCAL_KINDS:
        if experiment and experiment.primary_outcome not in {"qualified_organic_conversion_value", "qualified_organic_conversions"}:
            reasons.append("experiment_primary_outcome_must_measure_qualified_conversions")
        if not is_fixture and (site.conversion_definition or {}).get("verified") is not True:
            reasons.append("qualified_conversion_definition_required")
    gate = evaluate_policy(
        kind=revision.kind, autonomy_level=min(site.autonomy_level, max_autonomy_level) if max_autonomy_level is not None else site.autonomy_level,
        site_production_enabled=site.production_enabled, global_production_enabled=production_enabled,
        is_fixture=is_fixture, earned_categories=config.get("earned_categories", []),
        has_human_approval=_approval_valid(session, revision), verification_passed=not verification_reasons,
        evidence_valid=not evidence_reasons, has_experiment=valid_experiment,
        calibrated=not config.get("automation_suspended", False),
    )
    reasons.extend(gate.reasons)
    if revision.kind not in LOCAL_KINDS:
        reasons.extend(evidence_reasons)
        reasons.extend(verification_reasons)
    return sorted(set(reasons))


def _release_lease(session: Session, action_id: str) -> None:
    lease = session.scalar(select(ExecutionLease).where(ExecutionLease.action_id == action_id))
    if lease:
        session.delete(lease)


def _store_page_version(session: Session, page: Page, snapshot: CMSPage, action: Action) -> None:
    number = (session.scalar(select(func.max(PageVersion.version_number)).where(PageVersion.page_id == page.id)) or 0) + 1
    session.add(PageVersion(
        site_id=page.site_id, page_id=page.id, action_id=action.id, version_number=number,
        content_json=snapshot.model_dump(mode="json"), content_hash=snapshot.fingerprint,
    ))
    session.flush()


def execute_revision(
    session: Session, cms: CMSProvider, *, revision_id: str, actor: str,
    idempotency_key: str, production_enabled: bool = False, max_daily_actions: int | None = None,
    max_autonomy_level: int | None = None,
) -> dict[str, Any]:
    revision = session.get(Revision, revision_id)
    if revision is None:
        raise ValueError("unknown_revision")
    if not idempotency_key or len(idempotency_key) > 200 or not actor.strip():
        return _audit(session, site_id=revision.site_id, revision=revision, kind=revision.kind, actor=actor,
                      status="blocked", details={"reasons": ["bounded_idempotency_key_and_actor_required"]})
    binding = {"operation": "execute_revision", "revision_hash": revision.revision_hash,
               "provider_is_fixture": bool(cms.is_fixture)}
    existing = session.scalar(select(Action).where(Action.site_id == revision.site_id, Action.idempotency_key == idempotency_key))
    if existing:
        if existing.revision_id != revision.id or existing.payload_json != binding:
            return _audit(session, site_id=revision.site_id, revision=revision, kind=revision.kind, actor=actor,
                          status="blocked", details={"reasons": ["idempotency_key_bound_to_different_request"], "existing_action_id": existing.id})
        return _result(session, existing, replay=True)
    try:
        action = _new_action(session, site_id=revision.site_id, revision=revision, kind=revision.kind,
                             actor=actor, reason=revision.reason, key=idempotency_key, payload=binding)
        _event(session, action, "requested", {"revision_hash": revision.revision_hash})
        session.commit()  # Durable intent is required before any external mutation.
    except IntegrityError:
        session.rollback()
        # Only a proven duplicate key can be recovered as an idempotent replay.
        # Other storage constraint failures must never recurse or reach the CMS.
        duplicate = session.scalar(select(Action).where(Action.site_id == revision.site_id, Action.idempotency_key == idempotency_key))
        if duplicate is None:
            raise
        if duplicate.revision_id != revision.id or duplicate.payload_json != binding:
            return _audit(session, site_id=revision.site_id, revision=revision, kind=revision.kind, actor=actor,
                          status="blocked", details={"reasons": ["idempotency_key_bound_to_different_request"], "existing_action_id": duplicate.id})
        return _result(session, duplicate, replay=True)
    site = session.get(Site, revision.site_id, populate_existing=True)
    page = session.get(Page, revision.page_id)
    if site is None or page is None or page.site_id != revision.site_id:
        _event(session, action, "blocked", {"reasons": ["revision_scope_invalid"]})
        session.commit()
        return _result(session, action)
    reasons = _policy_reasons(session, revision, site, is_fixture=cms.is_fixture, production_enabled=production_enabled,
                              max_autonomy_level=max_autonomy_level)
    if reasons:
        _event(session, action, "blocked", {"reasons": reasons})
        session.commit()
        return _result(session, action)
    if revision.kind in LOCAL_KINDS:
        _event(session, action, "local_draft_created", {"revision_hash": revision.revision_hash, "remote_write": False})
        session.commit()
        return _result(session, action)
    try:
        prediction = _freeze_prediction(session, site, revision, action, is_fixture=cms.is_fixture)
        session.add(ExecutionLease(
            id=str(uuid4()), site_id=site.id, page_id=page.id, action_id=action.id,
            owner=actor, token=str(uuid4()),
        ))
        _event(session, action, "dispatching", {"before_hash": revision.before_hash,
            "prediction_evidence_id": prediction.id, "prediction_hash": prediction.content_hash})
        session.commit()  # Survives process crashes. It is never expired automatically.
    except IntegrityError:
        session.rollback()
        _event(session, action, "blocked", {"reasons": ["page_has_pending_action_or_reconciliation"]})
        session.commit()
        return _result(session, action)
    remote_write_started = False
    try:
        # Keep the site row locked through the write, preventing a policy change
        # between this final check and dispatch on PostgreSQL. The page lease
        # prevents concurrent remote writers even after a process crashes.
        site = session.scalar(select(Site).where(Site.id == revision.site_id).with_for_update().execution_options(populate_existing=True))
        session.expire(revision)
        reasons = _policy_reasons(session, revision, site, is_fixture=cms.is_fixture, production_enabled=production_enabled,
                                  max_autonomy_level=max_autonomy_level)
        recent_actions = session.scalar(select(func.count(Action.id)).where(
            Action.site_id == site.id, Action.kind.in_([kind.value for kind in ActionKind if kind not in LOCAL_KINDS]),
            Action.created_at >= utcnow() - timedelta(days=1),
        )) or 0
        limit = int((site.config_json or {}).get("max_daily_actions", 5))
        if max_daily_actions is not None:
            limit = min(limit, max_daily_actions)
        if recent_actions > limit:
            reasons.append("daily_action_budget_exhausted")
        current = cms.get_page(CMSPage.model_validate(revision.before_json).external_id)
        # A network read may consume time: recheck expiry, fresh verifier verdicts,
        # evidence and policy again after it, immediately ahead of dispatch.
        reasons.extend(_policy_reasons(session, revision, site, is_fixture=cms.is_fixture, production_enabled=production_enabled,
                                       max_autonomy_level=max_autonomy_level))
        if current.fingerprint != revision.before_hash:
            reasons.append("cms_changed_since_revision")
        if not cms.is_fixture and revision.kind != ActionKind.CREATE_CMS_DRAFT and current.metadata.get("atomic_compare_and_swap") is not True:
            reasons.append("production_adapter_requires_atomic_compare_and_swap")
        if reasons:
            _release_lease(session, action.id)
            _event(session, action, "blocked", {"reasons": sorted(set(reasons))})
            session.commit()
            return _result(session, action)
        before = CMSPage.model_validate(revision.before_json)
        intended = CMSPage.model_validate(revision.after_json)
        _store_page_version(session, page, current, action)
        # No database write can fail undiscovered before the first remote POST:
        # flush verifies storage constraints; durable command already committed.
        session.flush()
        remote_write_started = True
        if revision.kind == ActionKind.CREATE_CMS_DRAFT:
            after = cms.create_draft(intended.title, intended.content)
            fetched = cms.get_page(after.external_id)
            matches = fetched.status == "draft" and fetched.title == intended.title and fetched.content == intended.content
        else:
            changes = {key: value for key, value in revision.changes_json.items() if not key.startswith("__")}
            after = cms.update_page(before.external_id, changes, expected_fingerprint=before.fingerprint)
            fetched = cms.get_page(after.external_id)
            matches = fetched.fingerprint == intended.fingerprint and after.fingerprint == fetched.fingerprint
        if not matches:
            _event(session, action, "reconciliation_required", {"reasons": ["remote_result_did_not_match_approved_revision"], "observed_hash": fetched.fingerprint})
            session.commit()
            return _result(session, action)
        if revision.kind != ActionKind.CREATE_CMS_DRAFT:
            _store_page_version(session, page, fetched, action)
            page.title, page.content_html, page.meta_description = fetched.title, fetched.content, fetched.meta_description
            page.content_hash = fetched.fingerprint
            page.metadata_json = {**(page.metadata_json or {}), "cms_snapshot": fetched.model_dump(mode="json")}
        experiment = session.get(Experiment, revision.experiment_id)
        experiment.deployed_at = utcnow()
        experiment.status = "running"
        _event(session, action, "succeeded", {
            "before": current.model_dump(mode="json"), "after": fetched.model_dump(mode="json"),
            "before_hash": current.fingerprint, "after_hash": fetched.fingerprint,
            "actor": actor, "experiment_id": revision.experiment_id,
            "rollback_procedure": "propose exact inverse with rollback_action; independently verify and approve the new revision",
            "fixture": cms.is_fixture, "deployment_at": experiment.deployed_at.isoformat(),
            "prediction_evidence_id": prediction.id, "prediction_hash": prediction.content_hash,
        })
        _release_lease(session, action.id)
        session.commit()
        return _result(session, action)
    except ConcurrencyConflict:
        session.rollback()
        _release_lease(session, action.id)
        _event(session, action, "blocked", {"reasons": ["cms_atomic_precondition_failed"]})
        session.commit()
        return _result(session, action)
    except Exception as exc:
        session.rollback()
        # After dispatch begins every unfamiliar failure is ambiguous. Keep the
        # lease and never resend, even if the remote call appears to have failed.
        status = "reconciliation_required" if remote_write_started else "failed"
        if not remote_write_started:
            _release_lease(session, action.id)
        try:
            _event(session, action, status, {"error_type": type(exc).__name__, "remote_outcome": "unknown" if remote_write_started else "not_dispatched"})
            session.commit()
        except Exception:
            session.rollback()
            # The committed dispatching event and lease remain reconstructable
            # when database connectivity returns. Do not mask a storage failure.
            raise
        return _result(session, action)


def rollback_action(
    session: Session, cms: CMSProvider, *, action_id: str, actor: str,
    idempotency_key: str, production_enabled: bool = False, max_daily_actions: int | None = None,
    max_autonomy_level: int | None = None,
) -> dict[str, Any]:
    """Prepare an exact inverse; fresh verification and approval are mandatory.

    Calling again after approving the returned revision executes it through the
    identical gate. A new edit on the remote page blocks the rollback.
    """
    original = session.get(Action, action_id)
    if not original or not original.revision_id:
        raise ValueError("unknown_reversible_action")
    revision = session.get(Revision, original.revision_id)
    successes = [event for event in _events(session, original.id) if event.event_type == "succeeded"]
    if not successes or revision.kind == ActionKind.CREATE_CMS_DRAFT or revision.changes_json.get("__rollback_of"):
        return _audit(session, site_id=original.site_id, revision=revision, kind="rollback_action", actor=actor,
                      status="blocked", details={"reasons": ["action_is_not_a_supported_reversible_update"]})
    # Reuse the immutable reversal associated with this original action. Its hash
    # cannot be replaced to bypass the approval binding.
    reversals = session.scalars(select(Revision).where(Revision.site_id == original.site_id, Revision.page_id == revision.page_id)).all()
    reverse = next((item for item in reversals if item.changes_json.get("__rollback_of") == original.id), None)
    if reverse is None:
        before = CMSPage.model_validate(successes[-1].details_json["after"])
        after = CMSPage.model_validate(revision.before_json)
        experiment = Experiment(
            id=str(uuid4()), site_id=original.site_id, page_id=revision.page_id,
            name=f"Rollback {original.id}", hypothesis="Reversing the recorded change restores the previously verified state",
            mechanism="Exact inverse of one audited action", primary_outcome="qualified_organic_conversion_value",
            predicted_confidence=None, status="proposed", verdict="pending",
        )
        session.add(experiment)
        session.flush()
        reverse = Revision(
            id=str(uuid4()), site_id=original.site_id, page_id=revision.page_id, kind=revision.kind,
            changes_json={**_changes(before, after), "__rollback_of": original.id},
            before_json=before.model_dump(mode="json"), after_json=after.model_dump(mode="json"),
            before_hash=before.fingerprint, revision_hash="", evidence_ids_json=revision.evidence_ids_json,
            reason=f"Proposed exact rollback of action {original.id}; fresh verifier and approval required",
            created_by=actor, experiment_id=experiment.id,
        )
        reverse.revision_hash = _revision_digest(reverse)
        session.add(reverse)
        session.flush()
        session.add(RollbackEvent(site_id=original.site_id, action_id=original.id, reason=reverse.reason,
                                  actor=actor, status="proposed", details_json={"revision_id": reverse.id}))
        _audit(session, site_id=original.site_id, revision=reverse, kind="rollback_proposal", actor=actor,
               status="local_draft_created", details={"rollback_of": original.id})
        return {"status": "rollback_proposed", "revision_id": reverse.id, "revision_hash": reverse.revision_hash,
                "rollback_of": original.id, "requirements": ["fresh_independent_verification", "stored_approval_or_earned_level_two"]}
    return execute_revision(session, cms, revision_id=reverse.id, actor=actor,
                            idempotency_key=idempotency_key, production_enabled=production_enabled,
                            max_daily_actions=max_daily_actions, max_autonomy_level=max_autonomy_level)


def reconcile_action(session: Session, cms: CMSProvider, *, action_id: str, actor: str) -> dict[str, Any]:
    """Operator-scoped, read-only CMS reconciliation of an ambiguous update.

    Only the exact approved AFTER state proves enough to release the lease.
    Observing the BEFORE state does not prove a delayed request cannot still run.
    New CMS draft IDs cannot be recovered safely from content similarity and are
    left for operator investigation. This operation never submits a CMS write.
    """
    original = session.get(Action, action_id)
    revision = session.get(Revision, original.revision_id) if original and original.revision_id else None
    if original is None or revision is None:
        raise ValueError("unknown_reconcilable_action")
    request = _new_action(session, site_id=original.site_id, revision=revision, kind="reconcile_action",
                          actor=actor, reason=f"Read-only reconciliation of action {original.id}",
                          payload={"reconciles_action": original.id})
    _event(session, request, "requested", {"remote_write": False})
    session.commit()
    site = session.scalar(select(Site).where(Site.id == original.site_id).with_for_update().execution_options(populate_existing=True))
    lease = session.scalar(select(ExecutionLease).where(ExecutionLease.action_id == original.id).with_for_update())
    if lease is None:
        _event(session, request, "local_draft_created", {"outcome": "no_pending_lease", "reconciles_action": original.id})
        session.commit()
        result = _result(session, original, replay=True)
        result["reconciliation_action_id"] = request.id
        return result
    reasons = _revision_reasons(session, revision, site)
    if original.kind == ActionKind.CREATE_CMS_DRAFT:
        reasons.append("cms_draft_identity_requires_operator_investigation")
    if original.payload_json.get("provider_is_fixture") is not bool(cms.is_fixture):
        reasons.append("reconciliation_provider_mode_must_match_original")
    if reasons:
        _event(session, request, "blocked", {"reasons": sorted(set(reasons)), "lease_retained": True})
        session.commit()
        return _result(session, request)
    try:
        current = cms.get_page(CMSPage.model_validate(revision.before_json).external_id)
    except Exception as exc:
        _event(session, request, "failed", {"error_type": type(exc).__name__, "remote_write": False, "lease_retained": True})
        session.commit()
        return _result(session, request)
    expected_after = CMSPage.model_validate(revision.after_json)
    if current.fingerprint != expected_after.fingerprint:
        _event(session, request, "blocked", {
            "reasons": ["remote_state_does_not_prove_approved_write_completed"],
            "lease_retained": True, "observed_hash": current.fingerprint,
        })
        session.commit()
        return _result(session, request)
    page = session.get(Page, revision.page_id)
    before = CMSPage.model_validate(revision.before_json)
    _store_page_version(session, page, before, original)
    _store_page_version(session, page, current, original)
    page.title, page.content_html, page.meta_description = current.title, current.content, current.meta_description
    page.content_hash = current.fingerprint
    page.metadata_json = {**(page.metadata_json or {}), "cms_snapshot": current.model_dump(mode="json")}
    experiment = session.get(Experiment, revision.experiment_id)
    dispatch = next((event for event in _events(session, original.id) if event.event_type == "dispatching"), None)
    # The external write cannot precede the committed dispatch record. Intent
    # creation happened earlier and may even predate the frozen prediction.
    earliest_deployment = dispatch.created_at if dispatch else original.created_at
    experiment.status = "running"
    experiment.deployed_at = earliest_deployment
    experiment.analysis_json = {
        **(experiment.analysis_json or {}), "deployment_time_uncertain": True,
        "deployment_time_bounds": [earliest_deployment.isoformat(), utcnow().isoformat()],
    }
    _event(session, original, "succeeded", {
        "before": before.model_dump(mode="json"), "after": current.model_dump(mode="json"),
        "before_hash": before.fingerprint, "after_hash": current.fingerprint,
        "actor": original.actor, "experiment_id": revision.experiment_id,
        "rollback_procedure": "propose exact inverse with rollback_action; independently verify and approve the new revision",
        "fixture": cms.is_fixture, "reconciled_by": actor, "reconciliation_action_id": request.id,
        "deployment_time_uncertain": True,
        "deployment_at": experiment.deployed_at.isoformat(),
        "prediction_evidence_id": dispatch.details_json.get("prediction_evidence_id") if dispatch else None,
        "prediction_hash": dispatch.details_json.get("prediction_hash") if dispatch else None,
    })
    _event(session, request, "local_draft_created", {"outcome": "approved_after_state_confirmed", "remote_write": False, "reconciles_action": original.id})
    session.delete(lease)
    session.commit()
    result = _result(session, original)
    result["reconciliation_action_id"] = request.id
    return result
