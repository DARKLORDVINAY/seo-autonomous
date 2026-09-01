"""Bounded code-governed SDK analysis. This module has NO execution capability.

Integration boundary: evidence must be loaded from the canonical DB by a trusted
service. A caller must not promote metadata inside crawled content to source_trust.
Returned packets are proposals, never approvals or an instruction to execute them.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal
from uuid import uuid4

from opentelemetry import trace
from pydantic import BaseModel, Field, ValidationError

from backend.app.contracts import (
    ActionKind, CMSPage, ClaimType, FindingPacket, ProviderUnavailable, Risk, StrictModel,
    VerificationPacket, stable_hash, utcnow,
)
from .roles import BASE_POLICY, REQUIRED_VERIFIER_CHECKS, make_contract, select_specialists
from .metadata import METADATA_POLICY, MetadataDraftOutput, validate_metadata_draft

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RuntimeBudget(StrictModel):
    max_specialists: int = Field(default=3, ge=1, le=3)
    max_model_calls: int = Field(default=5, ge=1, le=5)
    per_call_timeout_seconds: float = Field(default=30, gt=0, le=60)
    max_cycle_seconds: float = Field(default=120, gt=0, le=300)
    max_output_tokens: int = Field(default=1800, ge=128, le=4096)
    max_input_bytes: int = Field(default=24000, ge=1024, le=64000)
    # Conservative byte-based input reservation + output allowance, NOT billed tokens.
    max_reserved_tokens: int = Field(default=160000, ge=1024, le=500000)


class VerificationCheck(StrictModel):
    name: str
    passed: bool
    reason: str


class VerifierOutput(StrictModel):
    """Strict Responses JSON schema; free-key dicts are unsupported in strict mode."""
    verdict: Literal["PASS", "BLOCK", "NEEDS_EVIDENCE"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
    evidence_ids: list[str]
    alternative_explanations: list[str]
    checks: list[VerificationCheck]
    action_safe: bool


class RevisionTarget(StrictModel):
    """Trusted caller supplies the stored revision; a model cannot choose it."""
    before: CMSPage
    after: CMSPage
    revision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuditSinkError(RuntimeError):
    """No result may progress if its configured audit sink failed."""


class BudgetExceeded(RuntimeError):
    pass


def _bound_json_input(value: Any, max_chars: int) -> None:
    """Bound strings, containers and depth before allocating a JSON encoding.

    The exact encoded-byte limit is still checked by the caller. This first pass
    avoids serialising a huge CMS snapshot or evidence collection just to reject
    it. Inputs must be JSON values; cycles also hit the depth bound.
    """
    pending, used = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        if depth > 32 or used > max_chars:
            raise BudgetExceeded("Input structure exceeds the bounded allowance")
        if isinstance(item, str):
            used += len(item) + 2
        elif isinstance(item, dict):
            used += 2 + len(item) * 2
            if used > max_chars:
                raise BudgetExceeded("Input structure exceeds the bounded allowance")
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("Model input keys must be strings")
                used += len(key) + 2
                if used > max_chars:
                    raise BudgetExceeded("Input string exceeds the bounded allowance")
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            used += 2 + len(item)
            if used > max_chars:
                raise BudgetExceeded("Input structure exceeds the bounded allowance")
            pending.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, bool):
            used += 5
        elif isinstance(item, (int, float)):
            if isinstance(item, int) and item.bit_length() > max_chars:
                raise BudgetExceeded("Input number exceeds the bounded allowance")
            used += len(str(item))
        else:
            raise ValueError("Model input must contain JSON values only")
    if used > max_chars:
        raise BudgetExceeded("Input string exceeds the bounded allowance")


HIGH_ACTIONS = {
    ActionKind.PUBLISH_PAGE, ActionKind.CHANGE_SLUG, ActionKind.CHANGE_CANONICAL,
    ActionKind.CHANGE_ROBOTS, ActionKind.REDIRECT_URL, ActionKind.DELETE_PAGE,
    ActionKind.MODIFY_TEMPLATE, ActionKind.DEPLOY_CODE,
}
ALLOWED_ACTIONS = {str(v) for v in ActionKind} | {"NO-ACTION", "investigate"}
TRUST_LABELS = {"trusted_measurement", "trusted_operator", "untrusted_external", "fixture"}
QUALITY_BLOCKERS = {"partial", "tracking_outage", "tracking_error", "unknown", "suppressed"}
TRUSTED_VERIFIER_ID = "sceptical-verifier:v1"


def _evidence_boundary(evidence: list[dict]) -> tuple[list[dict], list[str]]:
    clean, issues, seen = [], [], set()
    if len(evidence) > 128:
        return [], ["Evidence selection exceeds the 128-record bound; refine the task."]
    for item in evidence:
        if not isinstance(item, dict):
            issues.append("Canonical evidence must be a JSON object.")
            continue
        evidence_id = item.get("id")
        if (not isinstance(evidence_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", evidence_id)
                or evidence_id in seen):
            issues.append("Missing or duplicate canonical evidence ID.")
            continue
        seen.add(evidence_id)
        if (not isinstance(item.get("source_trust"), str) or item["source_trust"] not in TRUST_LABELS
                or not isinstance(item.get("source"), str) or not item["source"].strip()):
            issues.append("Canonical evidence lacks a recognised trust label or source.")
            continue
        clean.append(dict(item))
    if not clean:
        issues.append("No usable canonical evidence supplied.")
    return clean, issues


def _no_action(reason: str, evidence_ids: list[str] | None = None) -> FindingPacket:
    return FindingPacket(
        finding=reason, claim_type=ClaimType.INFERENCE, confidence=0,
        supporting_evidence=evidence_ids or [], recommended_action="NO-ACTION",
        uncertainty=[reason], needs_human_review=True,
    )


def _blocked(reason: str, verifier_id: str, verdict: str = "NEEDS_EVIDENCE") -> VerificationPacket:
    return VerificationPacket(
        verdict=verdict, verifier_id=verifier_id, independent=False, confidence=0,
        reasons=[reason], evidence_ids=[], checks={}, action_safe=False,
    )


def _blind_problem(problem: dict) -> dict:
    # Deliberately omit proposed action, rationale, ranking and prior agent conclusions.
    keys = {"kind", "page_url", "symptoms", "baseline", "current", "business_objective", "scope"}
    return {k: v for k, v in problem.items() if k in keys}


def _regret(proposal: FindingPacket | None = None) -> dict:
    high = proposal is not None and (
        proposal.risk in {Risk.HIGH, Risk.CRITICAL} or proposal.recommended_action in HIGH_ACTIONS
    )
    return {
        "false_positive": "site damage / conversion loss; establish magnitude from evidence",
        "false_negative": "foregone incremental qualified conversion value; currently unknown",
        "threshold": "blocked in this release" if high else "evidence of benefit + reversibility + independent review",
        "rule": "Reversibility lowers action regret; it does not establish that a change will help.",
    }


class AgentRuntime:
    """One runtime = one bounded cycle. No background work or hidden paid retries."""

    def __init__(
        self, *, mode: Literal["fixture", "live"] = "fixture", budget: RuntimeBudget | None = None,
        record_run: Callable[[dict], Any] | None = None,
        model: str | None = None, api_key: str | None = None,
    ):
        if mode not in {"fixture", "live"}:
            raise ValueError("mode must be fixture or live")
        self.mode, self.budget, self.record_run = mode, budget or RuntimeBudget(), record_run
        self.model = (model if model is not None else os.getenv("OPENAI_MODEL", "")).strip() if mode == "live" else None
        self._api_key = api_key.strip() if mode == "live" and api_key is not None else None
        configured_key = self._api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "").strip()
        if mode == "live" and (not self.model or not configured_key):
            raise ProviderUnavailable("Live agents require OPENAI_MODEL and OPENAI_API_KEY; fixture mode makes no API calls.")
        self.cycle_id, self.started = str(uuid4()), time.monotonic()
        self.verifier_id = TRUSTED_VERIFIER_ID
        self.runs: list[dict] = []
        self.model_calls = self.reserved_tokens = 0

    async def _record(self, run: dict, *, terminal: bool = True) -> None:
        # Sink packets are independent snapshots. The public summary contains
        # one terminal packet per invocation, never the start + finish twice.
        packet = deepcopy(run)
        logger.info("seo_agent_run", extra={"agent_run": packet})
        if self.record_run:
            try:
                value = self.record_run(deepcopy(packet))
                if inspect.isawaitable(value):
                    await value
            except Exception as exc:
                raise AuditSinkError("Agent audit sink failed; analysis must not progress.") from exc
        if terminal:
            self.runs.append(packet)

    async def _invoke(self, role: str, payload: dict, output_type: type[BaseModel]) -> BaseModel | None:
        contract = make_contract(role, [e["id"] for e in payload["evidence"]])
        run = {
            "id": str(uuid4()), "cycle_id": self.cycle_id, "role": role,
            "mode": self.mode, "model": self.model, "started_at": utcnow().isoformat(),
            "status": "started", "latency_ms": 0, "input_tokens": None, "output_tokens": None,
            "cost_usd": None, "error_id": None, "error_type": None, "llm_executed": False,
            "llm_attempted": False, "reserved_model_calls": 0, "reserved_tokens": 0,
            "contract": contract.model_dump(mode="json"), "trace_id": f"trace_{uuid4().hex}",
        }
        started = time.monotonic()
        result = None
        audit_sink_failed = False
        try:
            if self.mode == "fixture":
                run["status"] = "fixture"
                run["input_tokens"] = run["output_tokens"] = 0
                run["cost_usd"] = 0
                return None
            from agents import Agent, ModelSettings, RunConfig, Runner
            from agents.models.openai_provider import OpenAIProvider
            from openai import AsyncOpenAI

            instructions = BASE_POLICY + "\nTrusted task contract:\n" + contract.model_dump_json()
            if role == "verifier":
                instructions += (
                    "\nReturn the required verification checks with specific evidence-based reasons: "
                    + ", ".join(REQUIRED_VERIFIER_CHECKS)
                    + ". PASS requires every check, no unsupported evidence and a sound blind diagnosis."
                )
                if payload.get("revision_target"):
                    instructions += (
                        "\nInspect the actual revision_target.before and revision_target.after, bound to "
                        "revision_target.revision_hash. Verify the proposed content and every changed field; "
                        "a plausible rationale alone cannot establish factual accuracy or safety."
                    )
            elif role == "metadata_draft":
                instructions += METADATA_POLICY
            model_input = {"untrusted_data": payload}
            _bound_json_input(model_input, self.budget.max_input_bytes)
            encoded = json.dumps(model_input, sort_keys=True, ensure_ascii=True, allow_nan=False)
            input_bytes = len(encoded.encode())
            reserve = input_bytes + len(instructions.encode()) + len(json.dumps(output_type.model_json_schema()).encode())
            reserve += self.budget.max_output_tokens + 1024
            remaining = self.budget.max_cycle_seconds - (time.monotonic() - self.started)
            if (
                self.model_calls >= self.budget.max_model_calls
                or input_bytes > self.budget.max_input_bytes
                or self.reserved_tokens + reserve > self.budget.max_reserved_tokens
                or remaining <= 0
            ):
                raise BudgetExceeded("Bounded run allowance exhausted")
            timeout = min(self.budget.per_call_timeout_seconds, remaining)
            agent = Agent(
                name=role, instructions=instructions, model=self.model, tools=[], handoffs=[],
                output_type=output_type,
                model_settings=ModelSettings(max_tokens=self.budget.max_output_tokens, store=False),
            )
            run.update(reserved_model_calls=1, reserved_tokens=reserve)
            # The caller commits its reservation here. No SDK spend is possible
            # until the audit sink acknowledges this stable run ID. A crash after
            # acknowledgement conservatively leaves the reservation in place.
            await self._record(run, terminal=False)
            self.model_calls += 1
            self.reserved_tokens += reserve
            remaining = self.budget.max_cycle_seconds - (time.monotonic() - self.started)
            if remaining <= 0:
                raise BudgetExceeded("Cycle deadline elapsed while reserving a model call")
            timeout = min(self.budget.per_call_timeout_seconds, remaining)
            with tracer.start_as_current_span(f"seo.agent.{role}") as span:
                span.set_attribute("seo.cycle_id", self.cycle_id)
                span.set_attribute("seo.mode", self.mode)
                credentials = {"api_key": self._api_key} if self._api_key is not None else {}
                async with AsyncOpenAI(max_retries=0, timeout=timeout, **credentials) as client:
                    run["llm_attempted"] = True
                    response = await asyncio.wait_for(
                        Runner.run(
                            agent, encoded, max_turns=1,
                            run_config=RunConfig(
                                model_provider=OpenAIProvider(openai_client=client),
                                tracing_disabled=os.getenv("SEO_AGENT_TRACING", "false").lower() != "true",
                                trace_include_sensitive_data=False,
                                workflow_name="Spiral Max bounded SEO analysis", group_id=self.cycle_id,
                                trace_id=run["trace_id"],
                            ),
                        ), timeout=timeout,
                    )
            run["llm_executed"] = True
            usage = getattr(getattr(response, "context_wrapper", None), "usage", None)
            if usage is not None:
                run["input_tokens"] = getattr(usage, "input_tokens", None)
                run["output_tokens"] = getattr(usage, "output_tokens", None)
            raw = response.final_output
            if isinstance(raw, BaseModel):
                raw = raw.model_dump()
            result = output_type.model_validate_json(raw) if isinstance(raw, str) else output_type.model_validate(raw)
            if len(result.model_dump_json().encode()) > self.budget.max_output_tokens * 16:
                raise ValueError("Output size bound exceeded")
            run["status"] = "completed"
        except AuditSinkError:
            # Do not retry a failed reservation or proceed to another model call.
            # The original sink failure must remain visible to the caller.
            audit_sink_failed = True
            raise
        except asyncio.CancelledError:
            run.update(status="cancelled", error_id=str(uuid4()), error_type="CancelledError")
            raise
        except (asyncio.TimeoutError, ValidationError, ValueError, BudgetExceeded) as exc:
            result = None
            run.update(status="budget_exhausted" if isinstance(exc, BudgetExceeded) else "error",
                       error_id=str(uuid4()), error_type=type(exc).__name__)
        except Exception as exc:
            result = None
            # Provider errors can include credentials/HTML. Store type/ID, never raw exception text.
            run.update(status="error", error_id=str(uuid4()), error_type=type(exc).__name__)
        finally:
            run["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
            run["completed_at"] = utcnow().isoformat()
            if not audit_sink_failed:
                await self._record(run)
        return result

    def _validate_finding(self, packet: FindingPacket, evidence: list[dict]) -> tuple[FindingPacket, str]:
        ids = {item["id"] for item in evidence}
        cited = set(packet.supporting_evidence + packet.contradicting_evidence)
        if cited - ids or not packet.supporting_evidence:
            return _no_action("NEEDS_EVIDENCE: missing or unknown canonical evidence IDs."), "NEEDS_EVIDENCE"
        if packet.recommended_action not in ALLOWED_ACTIONS:
            return _no_action("Unsupported action name; no execution permitted."), "NEEDS_EVIDENCE"
        if packet.claim_type in {ClaimType.FACT, ClaimType.ACTION}:
            packet = packet.model_copy(update={
                "claim_type": ClaimType.INFERENCE,
                "uncertainty": [*packet.uncertainty, "Generated interpretation is not an independently verified fact or action."],
            })
        return packet, "PROPOSAL"

    def _payload(self, problem: dict, evidence: list[dict], prior_failures: list[dict]) -> dict:
        return {
            "problem": problem, "evidence": evidence, "prior_failure_cases": prior_failures,
            "failure_history_status": "provided" if prior_failures else "not supplied; no inference of a clean record",
            "decision_regret": _regret(),
        }

    async def draft_metadata(
        self, problem: dict, evidence: list[dict], *, prior_failures: list[dict] | None = None,
    ) -> dict:
        """One model proposal for an immutable before-state, never a revision/write."""
        evidence, issues = _evidence_boundary(evidence)
        before_fingerprint, proposal = None, None
        try:
            before = CMSPage.model_validate(problem.get("before")).model_copy(deep=True)
            draft_problem = {**_blind_problem(problem), "before": before.model_dump(mode="json")}
            payload = self._payload(draft_problem, evidence, prior_failures or [])
            # Bound the snapshot before fingerprinting or serialising its content.
            _bound_json_input({"untrusted_data": payload}, self.budget.max_input_bytes)
            before_fingerprint = before.fingerprint
        except (AttributeError, ValidationError, ValueError, BudgetExceeded):
            issues.append("A valid, bounded immutable CMSPage before-state is required.")
        if not issues and self.mode == "live":
            if before.metadata.get("provider") == "fixture" or all(e["source_trust"] == "fixture" for e in evidence):
                issues.append("Fixture state cannot support a live metadata proposal.")
            if problem.get("page_url") and problem["page_url"] != before.url:
                issues.append("Problem URL does not match the immutable before-state.")
        if not issues:
            raw = await self._invoke("metadata_draft", payload, MetadataDraftOutput)
            if raw is not None and self.mode == "live":
                proposal = validate_metadata_draft(raw, before, evidence)
        return {
            "mode": self.mode, "llm_executed": any(run["llm_executed"] for run in self.runs),
            "status": "PROPOSAL" if proposal is not None else "NEEDS_EVIDENCE",
            "proposal": proposal, "before_fingerprint": before_fingerprint, "runs": self.runs,
        }

    async def analyze_problem(self, problem: dict, evidence: list[dict], *, prior_failures: list[dict] | None = None) -> dict:
        evidence, boundary_issues = _evidence_boundary(evidence)
        failures = prior_failures or []
        findings = []
        payload = self._payload(_blind_problem(problem), evidence, failures)
        for role in select_specialists(problem, self.budget.max_specialists):
            raw = None if boundary_issues else await self._invoke(role, payload, FindingPacket)
            if raw is None:
                why = "Fixture simulation: no LLM execution or production verification." if self.mode == "fixture" else "Agent unavailable or evidence/budget invalid."
                packet, status = _no_action(why), "NEEDS_EVIDENCE"
            else:
                packet, status = self._validate_finding(raw, evidence)
            findings.append({"role": role, "packet": packet.model_dump(mode="json"), "status": status})
        valid = [f for f in findings if f["status"] == "PROPOSAL"]
        # Never select a proposal merely because its author self-reports more confidence.
        # Material disagreement requires the governor to resolve a canonical contradiction.
        recommendations = {f["packet"]["recommended_action"] for f in valid}
        disagreement = len(recommendations) > 1
        selected = valid[0] if valid and not disagreement else None
        proposal = FindingPacket.model_validate(selected["packet"]) if selected else _no_action("No supported proposal.")
        if disagreement:
            verified = {"blind_review": None, "verification": _blocked(
                "Specialists disagree about the action; resolve the contradiction before proceeding.",
                self.verifier_id).model_dump(mode="json")}
        elif boundary_issues:
            verified = {"blind_review": None, "verification": _blocked(
                "; ".join(boundary_issues), self.verifier_id).model_dump(mode="json")}
        else:
            verified = await self.verify_proposal(
                problem, proposal, evidence, proposer_id=selected["role"] if selected else "governor",
                prior_failures=failures,
            )
        verdict = verified["verification"]["verdict"]
        decision = proposal.recommended_action if verdict == "PASS" else "NO-ACTION"
        return {
            "cycle_id": self.cycle_id, "mode": self.mode,
            "llm_executed": any(r["llm_executed"] for r in self.runs),
            "status": "PROPOSAL" if verdict == "PASS" else verdict,
            "findings": findings, "blind_review": verified["blind_review"],
            "verification": verified["verification"], "decision": decision,
            "decision_is_execution_authority": False,
            "evidence_issues": boundary_issues, "runs": self.runs,
            "budget": {**self.budget.model_dump(), "model_calls": self.model_calls, "reserved_tokens": self.reserved_tokens},
            "limitations": ["Confidence is uncalibrated; consult canonical calibration history.",
                            "Separate contexts reduce anchoring, not shared-model correlated errors."],
        }

    async def verify_proposal(
        self, problem: dict, proposal: FindingPacket | dict, evidence: list[dict], *, proposer_id: str,
        prior_failures: list[dict] | None = None, revision_target: dict | None = None,
    ) -> dict:
        proposal = FindingPacket.model_validate(proposal)
        evidence, issues = _evidence_boundary(evidence)
        blind_problem, target = _blind_problem(problem), None
        if revision_target is not None:
            try:
                _bound_json_input(revision_target, self.budget.max_input_bytes)
                target = RevisionTarget.model_validate(revision_target).model_copy(deep=True).model_dump(mode="json")
                blind_problem["baseline"] = target["before"]
                # With an explicit revision target, "current" might contain the
                # proposed after-state. It belongs only in the final review.
                blind_problem.pop("current", None)
            except (ValidationError, ValueError, BudgetExceeded):
                issues.append("A valid, bounded stored revision target is required.")
        payload = self._payload(blind_problem, evidence, prior_failures or [])
        blind = None
        if proposer_id == self.verifier_id or proposer_id in {"verifier", "verifier_blind"}:
            verification = _blocked("Proposer cannot act as its own independent verifier.", self.verifier_id, "BLOCK")
            verification.independent = False
        elif proposal.risk in {Risk.HIGH, Risk.CRITICAL} or proposal.recommended_action in HIGH_ACTIONS:
            verification = _blocked("High/critical actions are unsupported in this release.", self.verifier_id, "BLOCK")
        elif issues:
            verification = _blocked("; ".join(issues), self.verifier_id)
        else:
            # First call has neither proposal nor proposer rationale/history.
            blind_raw = await self._invoke("verifier_blind", payload, FindingPacket)
            if blind_raw is not None:
                blind, blind_status = self._validate_finding(blind_raw, evidence)
            else:
                blind_status = "NEEDS_EVIDENCE"
            if self.mode == "fixture":
                verification = _blocked("Fixture simulation cannot provide independent model verification.", self.verifier_id)
            elif blind_status != "PROPOSAL":
                verification = _blocked("Blind diagnosis failed or lacks usable evidence.", self.verifier_id)
            else:
                payload.update(proposal=proposal.model_dump(mode="json"),
                               blind_diagnosis=blind.model_dump(mode="json"), decision_regret=_regret(proposal))
                if target is not None:
                    payload["revision_target"] = target
                raw = await self._invoke("verifier", payload, VerifierOutput)
                verification = self._final_verification(raw, proposal, evidence)
        return {
            "mode": self.mode, "llm_executed": any(r["llm_executed"] for r in self.runs),
            "blind_review": blind.model_dump(mode="json") if blind else None,
            "verification": verification.model_dump(mode="json"),
            "proposal_hash": stable_hash(proposal), "runs": self.runs,
            "decision_is_execution_authority": False,
        }

    def _final_verification(self, raw: VerifierOutput | None, proposal: FindingPacket, evidence: list[dict]) -> VerificationPacket:
        if raw is None:
            return _blocked("Verifier output unavailable or invalid.", self.verifier_id)
        ids = {e["id"] for e in evidence}
        cited = set(raw.evidence_ids + proposal.supporting_evidence + proposal.contradicting_evidence)
        if not raw.evidence_ids or not proposal.supporting_evidence or cited - ids:
            return _blocked("Unknown or missing evidence IDs in proposal or verifier.", self.verifier_id)
        if proposal.recommended_action not in ALLOWED_ACTIONS:
            return _blocked("Unsupported proposed action.", self.verifier_id, "BLOCK")
        checks = {check.name: check.passed and bool(check.reason.strip()) for check in raw.checks}
        if len(checks) != len(raw.checks):
            return _blocked("Duplicate verifier checks are ambiguous.", self.verifier_id)
        blockers = any(
            e["id"] in cited and (
                e["source_trust"] == "fixture" or e.get("data_state") in QUALITY_BLOCKERS
                or set(e.get("quality_flags", [])) & QUALITY_BLOCKERS
            ) for e in evidence
        )
        if raw.verdict == "PASS" and (
            blockers or not all(checks.get(name, False) for name in REQUIRED_VERIFIER_CHECKS)
            or not raw.action_safe or not raw.reasons or not raw.alternative_explanations
        ):
            return _blocked("PASS lacks complete independent checks or uses incomplete/fixture observations.", self.verifier_id)
        return VerificationPacket(
            verdict=raw.verdict, verifier_id=self.verifier_id, independent=True,
            confidence=raw.confidence, reasons=raw.reasons, evidence_ids=raw.evidence_ids,
            alternative_explanations=raw.alternative_explanations, checks=checks,
            action_safe=raw.action_safe and raw.verdict == "PASS",
        )


async def analyze_problem(
    problem: dict, evidence: list[dict], *, mode: Literal["fixture", "live"] = "fixture",
    prior_failures: list[dict] | None = None, budget: RuntimeBudget | None = None,
    record_run: Callable[[dict], Any] | None = None,
    model: str | None = None, api_key: str | None = None,
) -> dict:
    return await AgentRuntime(mode=mode, budget=budget, record_run=record_run, model=model, api_key=api_key).analyze_problem(
        problem, evidence, prior_failures=prior_failures,
    )


async def verify_proposal(
    problem: dict, proposal: FindingPacket | dict, evidence: list[dict], *, proposer_id: str,
    mode: Literal["fixture", "live"] = "fixture", prior_failures: list[dict] | None = None,
    budget: RuntimeBudget | None = None, record_run: Callable[[dict], Any] | None = None,
    revision_target: dict | None = None, model: str | None = None, api_key: str | None = None,
) -> dict:
    return await AgentRuntime(mode=mode, budget=budget, record_run=record_run, model=model, api_key=api_key).verify_proposal(
        problem, proposal, evidence, proposer_id=proposer_id, prior_failures=prior_failures,
        revision_target=revision_target,
    )


async def draft_metadata(
    problem: dict, evidence: list[dict], *, mode: Literal["fixture", "live"] = "fixture",
    prior_failures: list[dict] | None = None, record_run: Callable[[dict], Any] | None = None,
    budget: RuntimeBudget | None = None,
    model: str | None = None, api_key: str | None = None,
) -> dict:
    return await AgentRuntime(
        mode=mode, budget=budget or RuntimeBudget(max_model_calls=1), record_run=record_run, model=model, api_key=api_key,
    ).draft_metadata(problem, evidence, prior_failures=prior_failures)
