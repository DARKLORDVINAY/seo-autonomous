"""Fail-closed, deterministic action validation.

This module deliberately does not import agents. External content, inferred risk,
and model confidence cannot change these rules. It is safe to call during proposal
and is always called again immediately before execution.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from html import unescape
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from backend.app.contracts import ActionKind, CMSPage, Risk

LOCAL_KINDS = frozenset({
    ActionKind.CREATE_CONTENT_DRAFT, ActionKind.CREATE_METADATA_DRAFT, ActionKind.PROPOSE_INTERNAL_LINK,
})
LEVEL2_KINDS = frozenset({
    ActionKind.UPDATE_TITLE, ActionKind.UPDATE_META_DESCRIPTION, ActionKind.ADD_INTERNAL_LINK,
    ActionKind.UPDATE_SCHEMA, ActionKind.CREATE_CMS_DRAFT,
})
SUPPORTED_KINDS = LOCAL_KINDS | LEVEL2_KINDS | {ActionKind.UPDATE_EXISTING_COPY}
_RISK = {
    **dict.fromkeys(LOCAL_KINDS, Risk.LOW),
    **dict.fromkeys(LEVEL2_KINDS, Risk.MEDIUM),
    ActionKind.UPDATE_EXISTING_COPY: Risk.MEDIUM,
    ActionKind.PUBLISH_PAGE: Risk.HIGH,
    ActionKind.CHANGE_SLUG: Risk.HIGH,
    ActionKind.CHANGE_CANONICAL: Risk.HIGH,
    ActionKind.REDIRECT_URL: Risk.HIGH,
    ActionKind.CHANGE_ROBOTS: Risk.CRITICAL,
    ActionKind.DELETE_PAGE: Risk.CRITICAL,
    ActionKind.MODIFY_TEMPLATE: Risk.CRITICAL,
    ActionKind.DEPLOY_CODE: Risk.CRITICAL,
}
_ALLOWED_TAGS = frozenset({"p", "a", "strong", "em", "b", "i", "u", "ul", "ol", "li", "h2", "h3", "h4", "h5", "h6", "blockquote", "br", "span", "div", "code", "pre", "hr"})


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    risk: Risk
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "risk": self.risk.value, "reasons": list(self.reasons)}


def classify_risk(kind: ActionKind | str) -> Risk:
    """Unknown commands receive the highest risk, never a client-selected risk."""
    try:
        return _RISK[ActionKind(kind)]
    except (KeyError, ValueError, TypeError):
        return Risk.CRITICAL


def _valid_url(url: str, *, base_url: str | None = None, same_site: bool = False) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        return False
    decoded = unescape(url).strip()
    if "\\" in decoded:
        return False
    try:
        full = urljoin(base_url or "", decoded)
        parsed = urlsplit(full)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    if same_site:
        base = urlsplit(base_url or "")
        return (parsed.scheme, parsed.hostname, parsed.port) == (base.scheme, base.hostname, base.port)
    return True


def validate_safe_html(content: str, *, base_url: str) -> list[str]:
    """Small allowlist suitable for bounded text edits, with no active resources.

    Images, iframes, forms, styles, SVG/MathML, remote scripts, event handlers,
    data attributes, and comments are intentionally outside the first release.
    Existing untouched HTML need not pass this check for title/description edits.
    """
    if len(content) > 500_000:
        return ["content_size_exceeds_limit"]
    soup = BeautifulSoup(content, "html.parser")
    reasons: list[str] = []
    if re.search(r"\[\s*/?\s*[a-zA-Z][^\]]*\]", content):
        reasons.append("cms_shortcodes_not_allowed_in_edits")
    if soup.find_all(string=lambda t: isinstance(t, Comment)):
        reasons.append("html_comments_are_untrusted_and_not_allowed_in_edits")
    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_TAGS:
            reasons.append(f"unsafe_html_tag:{tag.name}")
        allowed_attrs = {"href", "title"} if tag.name == "a" else set()
        for attr in tag.attrs:
            if attr not in allowed_attrs:
                reasons.append(f"unsafe_html_attribute:{attr}")
        if tag.name == "a":
            href = tag.get("href")
            if not isinstance(href, str) or not _valid_url(href, base_url=base_url):
                reasons.append("unsafe_link_url")
    return sorted(set(reasons))


def _internal_link_check(before: str, after: str, base_url: str) -> list[str]:
    before_dom = BeautifulSoup(before, "html.parser")
    after_dom = BeautifulSoup(after, "html.parser")
    before_links = Counter(str(a) for a in before_dom.find_all("a"))
    after_links = Counter(str(a) for a in after_dom.find_all("a"))
    reasons: list[str] = []
    if before_links - after_links:
        reasons.append("internal_link_action_removed_or_changed_existing_link")
    added = list((after_links - before_links).elements())
    if len(added) != 1:
        reasons.append("internal_link_action_requires_exactly_one_added_link")
    for html in added:
        a = BeautifulSoup(html, "html.parser").find("a")
        if a is None or not a.get_text(strip=True) or not _valid_url(a.get("href", ""), base_url=base_url, same_site=True):
            reasons.append("added_link_must_have_text_and_same_site_target")
    for anchor in before_dom.find_all("a"):
        anchor.unwrap()
    for anchor in after_dom.find_all("a"):
        anchor.unwrap()
    if str(before_dom) != str(after_dom):
        reasons.append("internal_link_action_may_only_wrap_existing_text")
    return reasons


def _schema_check(value: Any, *, base_url: str) -> list[str]:
    reasons: list[str] = []
    allowed_types = {"Organization", "LocalBusiness", "ProfessionalService", "WebPage", "WebSite", "BreadcrumbList", "ListItem", "PostalAddress", "Service"}
    if not isinstance(value, (dict, list)):
        return ["schema_must_be_json_object_or_list"]
    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12:
            reasons.append("schema_too_deep")
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"aggregateRating", "review", "reviewRating", "ratingValue"}:
                    reasons.append("schema_reviews_and_ratings_require_unsupported_policy")
                if key == "@context" and item not in ("https://schema.org", "https://schema.org/"):
                    reasons.append("schema_remote_context_not_allowed")
                if key == "@type" and (not isinstance(item, str) or item not in allowed_types):
                    reasons.append("schema_type_not_allowlisted")
                if key in {"@id", "url"} and (not isinstance(item, str) or not _valid_url(item, base_url=base_url, same_site=True)):
                    reasons.append("schema_identity_must_be_same_site")
                if key in {"potentialAction", "target", "sameAs"}:
                    reasons.append("schema_action_or_external_identity_not_supported")
                walk(item, depth + 1)
        elif isinstance(node, list):
            if len(node) > 100:
                reasons.append("schema_list_too_large")
            for item in node[:101]:
                walk(item, depth + 1)
        elif isinstance(node, str):
            if len(node) > 4000 or "<" in node or ">" in node:
                reasons.append("schema_contains_html_or_overlong_string")
        elif node is not None and not isinstance(node, (int, float, bool)):
            reasons.append("schema_non_json_value")
    walk(value)
    return sorted(set(reasons))


def validate_revision(kind: ActionKind | str, before: CMSPage, after: CMSPage, *, base_url: str) -> GateDecision:
    risk = classify_risk(kind)
    reasons: list[str] = []
    try:
        action_kind = ActionKind(kind)
    except (ValueError, TypeError):
        return GateDecision(False, risk, ("unsupported_action_kind",))
    if action_kind not in SUPPORTED_KINDS:
        return GateDecision(False, risk, ("high_and_critical_actions_not_implemented",))
    if not _valid_url(before.url, base_url=base_url, same_site=True):
        reasons.append("revision_page_is_outside_site")
    old = before.model_dump(exclude={"modified_gmt"})
    new = after.model_dump(exclude={"modified_gmt"})
    changed = {key for key in old if old[key] != new[key]}
    allowed_fields = {
        ActionKind.UPDATE_TITLE: {"title"},
        ActionKind.CREATE_METADATA_DRAFT: {"title", "meta_description"},
        ActionKind.UPDATE_META_DESCRIPTION: {"meta_description"},
        ActionKind.ADD_INTERNAL_LINK: {"content"},
        ActionKind.PROPOSE_INTERNAL_LINK: {"content"},
        ActionKind.UPDATE_SCHEMA: {"metadata"},
        ActionKind.UPDATE_EXISTING_COPY: {"content"},
        ActionKind.CREATE_CONTENT_DRAFT: {"content", "title"},
        ActionKind.CREATE_CMS_DRAFT: {"title", "content", "status"},
    }[action_kind]
    if changed - allowed_fields:
        reasons.append("revision_changes_fields_outside_capability:" + ",".join(sorted(changed - allowed_fields)))
    if not changed:
        reasons.append("no_effective_change")
    if "title" in changed:
        if not after.title.strip() or len(after.title) > 180 or any(x in after.title for x in ("<", ">", "\n", "\r")):
            reasons.append("title_must_be_bounded_plain_text")
    if "meta_description" in changed:
        if len(after.meta_description) > 500 or any(x in after.meta_description for x in ("<", ">", "\n", "\r")):
            reasons.append("meta_description_must_be_bounded_plain_text")
    if "content" in changed:
        reasons.extend(validate_safe_html(after.content, base_url=base_url))
        if not after.content.strip():
            reasons.append("content_cannot_be_emptied")
    if action_kind in {ActionKind.ADD_INTERNAL_LINK, ActionKind.PROPOSE_INTERNAL_LINK}:
        reasons.extend(_internal_link_check(before.content, after.content, base_url))
    if action_kind == ActionKind.UPDATE_SCHEMA:
        old_metadata = {k: v for k, v in before.metadata.items() if k != "schema"}
        new_metadata = {k: v for k, v in after.metadata.items() if k != "schema"}
        if old_metadata != new_metadata or "schema" not in after.metadata:
            reasons.append("schema_action_may_only_change_schema_metadata")
        reasons.extend(_schema_check(after.metadata.get("schema"), base_url=base_url))
    if action_kind == ActionKind.CREATE_CMS_DRAFT and after.status != "draft":
        reasons.append("cms_draft_must_remain_draft")
    return GateDecision(not reasons, risk, tuple(sorted(set(reasons))))


def evaluate_policy(
    *, kind: ActionKind | str, autonomy_level: int, site_production_enabled: bool,
    global_production_enabled: bool, is_fixture: bool, earned_categories: list[str] | None = None,
    has_human_approval: bool = False, verification_passed: bool = False,
    evidence_valid: bool = False, has_experiment: bool = False, calibrated: bool = True,
) -> GateDecision:
    risk = classify_risk(kind)
    reasons: list[str] = []
    try:
        action_kind = ActionKind(kind)
    except (ValueError, TypeError):
        return GateDecision(False, risk, ("unsupported_action_kind",))
    if action_kind not in SUPPORTED_KINDS:
        reasons.append("high_and_critical_actions_not_implemented")
    if autonomy_level not in {1, 2}:
        reasons.append("only_levels_one_and_two_can_mutate_in_this_release")
    remote = action_kind not in LOCAL_KINDS
    if remote:
        if not is_fixture and not (global_production_enabled and site_production_enabled):
            reasons.append("production_mutations_disabled")
        if not verification_passed:
            reasons.append("independent_verification_required")
        if not evidence_valid:
            reasons.append("verified_evidence_provenance_required")
        if not has_experiment:
            reasons.append("experiment_required")
        auto_allowed = autonomy_level == 2 and action_kind in LEVEL2_KINDS and action_kind.value in (earned_categories or []) and calibrated
        if not has_human_approval and not auto_allowed:
            reasons.append("stored_human_approval_required")
    return GateDecision(not reasons, risk, tuple(sorted(set(reasons))))
