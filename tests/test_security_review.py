"""Independent adversarial regressions: no real credentials or network calls.

The CMS double advertises live mode to exercise production gates, but its state
exists only in this test process. Verifier packets enter through the documented
trusted service boundary; callers cannot submit such packets through MCP.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from threading import RLock
from uuid import uuid4

import httpcore
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.app.config.settings import Settings
from backend.app.contracts import CMSPage, ConcurrencyConflict, VerificationPacket, stable_hash, utcnow
from backend.app.db.models import Approval, Base, Evidence, Experiment, Page, Revision, Site
from backend.app.db.session import make_engine, make_session_factory
from backend.app.integrations.crawler.client import Crawler
from backend.app.integrations.crawler.network import PublicNetworkBackend, UnsafeURL, validate_url
from backend.app.services.execution import REQUIRED_CHECKS, approve_revision, execute_revision, propose_revision, record_verification
from seo_mcp.auth import PinnedJWTVerifier
from seo_mcp.server import ControlClient, create_server


class MemoryCMS:
    """Simulated live CMS: exercises live gates without HTTP or a real site."""
    is_fixture = False

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.writes = 0
        self.lock = RLock()

    def get_page(self, external_id):
        assert external_id == self.snapshot.external_id
        return self.snapshot.model_copy(deep=True)

    def update_page(self, external_id, changes, *, expected_fingerprint):
        with self.lock:
            assert external_id == self.snapshot.external_id
            if self.snapshot.fingerprint != expected_fingerprint:
                raise ConcurrencyConflict("Concurrent editorial update")
            self.snapshot = self.snapshot.model_copy(update=changes, deep=True)
            self.writes += 1
            return self.snapshot.model_copy(deep=True)

    def create_draft(self, *args, **kwargs):
        raise AssertionError("Local drafts must never call a CMS")


@dataclass
class ReviewCase:
    session: object
    site: Site
    page: Page
    cms: MemoryCMS
    experiment: Experiment

    def ready(self, *, content=None, kind="update_title", title="Accurate service description", evidence_site_id=None):
        observation = {"data_state": "final"} if content is None else content
        evidence = Evidence(site_id=evidence_site_id or self.site.id, source_type="gsc", source="gsc://canonical",
                            owner="ingestion", content=observation, content_hash=stable_hash(observation),
                            confidence=1, is_fixture=False)
        self.session.add(evidence)
        self.session.commit()
        proposed = self.cms.snapshot.model_copy(update={"title": title}, deep=True)
        result = propose_revision(self.session, site_id=self.site.id, page_id=self.page.id,
                                  kind=kind, after=proposed, created_by="content-specialist", reason="Bounded truthful title correction",
                                  evidence_ids=[evidence.id], experiment_id=self.experiment.id)
        if result["status"] == "blocked":
            return result
        revision_id = result["revision_id"]
        record_verification(self.session, revision_id=revision_id, packet=VerificationPacket(
            verdict="PASS", verifier_id="independent-reviewer", independent=True, confidence=.9,
            reasons=["Exact proposed diff reviewed against canonical business facts"], evidence_ids=[evidence.id],
            checks={name: True for name in REQUIRED_CHECKS}, action_safe=True,
            alternative_explanations=["Demand variation may explain the observed click trend"]))
        approve_revision(self.session, revision_id=revision_id, approved_by="human-owner", reason="Reviewed exact revision")
        return result

    def execute(self, revision_id, key=None):
        return execute_revision(self.session, self.cms, revision_id=revision_id, actor="executor",
                                idempotency_key=key or str(uuid4()), production_enabled=True)


@pytest.fixture
def review_case():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = Site(name="Security test", base_url="https://example.com", autonomy_level=1, production_enabled=True,
                    conversion_definition={"verified": True, "confirmed": True},
                    config_json={"trusted_verifier_ids": ["independent-reviewer"], "max_daily_actions": 50})
        session.add(site)
        session.flush()
        snapshot = CMSPage(external_id="pages:1", url="https://example.com/service", title="Service",
                           content="<p>Truthful service information.</p>", metadata={"atomic_compare_and_swap": True})
        page = Page(site_id=site.id, external_id=snapshot.external_id, url=snapshot.url, title=snapshot.title,
                    content_html=snapshot.content, metadata_json={"cms_snapshot": snapshot.model_dump(mode="json")})
        session.add(page)
        session.flush()
        experiment = Experiment(site_id=site.id, page_id=page.id, hypothesis="Accurate title improves qualified conversions",
                                primary_outcome="qualified_organic_conversion_value")
        session.add(experiment)
        session.commit()
        yield ReviewCase(session, site, page, MemoryCMS(snapshot), experiment)
    engine.dispose()


def test_review_harness_control_executes_once_with_complete_evidence(review_case):
    revision = review_case.ready()
    first = review_case.execute(revision["revision_id"], "once")
    replay = review_case.execute(revision["revision_id"], "once")
    assert first["status"] == "succeeded", first
    assert replay["idempotent_replay"] is True
    assert review_case.cms.writes == 1


@pytest.mark.parametrize("observation", [
    {"rows": [{"data_state": "partial", "clicks": 10}], "complete": True, "quality_flags": []},
    {"rows": [{"data_state": "unknown", "clicks": 10}], "complete": True, "quality_flags": []},
    {"rows": [], "complete": False, "quality_flags": ["tracking_outage"]},
    {"rows": [], "complete": False, "quality_flags": ["privacy_thresholding"]},
    {"rows": [{"data_state": "final"}], "complete": True, "metadata": {"tracking_outage": True}},
])
def test_batch_quality_defects_cannot_authorise_live_write(review_case, observation):
    """A mistaken model PASS cannot override deterministic data-quality failures."""
    revision = review_case.ready(content=observation)
    result = review_case.execute(revision["revision_id"])
    assert result["status"] == "blocked", result
    assert review_case.cms.writes == 0


def test_foreign_evidence_cannot_authorise_another_site(review_case):
    other = Site(name="Other tenant", base_url="https://elsewhere.example")
    review_case.session.add(other)
    review_case.session.commit()
    result = review_case.ready(evidence_site_id=other.id)
    assert result["status"] == "blocked"
    assert "evidence_not_in_site" in result["details"]["reasons"]
    assert review_case.cms.writes == 0


def test_local_metadata_draft_cannot_be_relabelled_as_remote_write(review_case):
    revision = review_case.ready(kind="create_metadata_draft")
    result = review_case.execute(revision["revision_id"])
    assert result["status"] == "local_draft_created"
    assert review_case.cms.writes == 0
    assert review_case.experiment.deployed_at is None


def test_latest_revocation_invalidates_existing_approval(review_case):
    proposed = review_case.ready()
    revision = review_case.session.get(Revision, proposed["revision_id"])
    review_case.session.add(Approval(site_id=review_case.site.id, revision_id=revision.id,
                                     revision_hash=revision.revision_hash, approved_by="human-owner",
                                     decision="REVOKE", reason="Owner withdrew approval", expires_at=utcnow()+timedelta(days=1)))
    review_case.session.commit()
    result = review_case.execute(revision.id)
    assert result["status"] == "blocked"
    assert "stored_human_approval_required" in result["details"]["reasons"]
    assert review_case.cms.writes == 0


@pytest.mark.parametrize("decision", ["REJECT", "REVOKE"])
def test_explicit_human_veto_also_blocks_earned_level_two(review_case, decision):
    """Earned automatic authority cannot supersede a later explicit human veto."""
    proposed = review_case.ready()
    revision = review_case.session.get(Revision, proposed["revision_id"])
    review_case.site.autonomy_level = 2
    review_case.site.config_json = {**review_case.site.config_json, "earned_categories": ["update_title"]}
    review_case.session.add(Approval(site_id=review_case.site.id, revision_id=revision.id,
                                     revision_hash=revision.revision_hash, approved_by="human-owner",
                                     decision=decision, reason="Do not deploy this revision", expires_at=utcnow()+timedelta(days=1)))
    review_case.session.commit()
    result = review_case.execute(revision.id)
    assert result["status"] == "blocked", result
    assert review_case.cms.writes == 0


def test_changed_stored_revision_fails_database_immutability(review_case):
    proposed = review_case.ready()
    with pytest.raises(DBAPIError, match="append-only"):
        review_case.session.execute(text("UPDATE revisions SET revision_hash = :hash WHERE id = :id"),
                                    {"hash": "0"*64, "id": proposed["revision_id"]})
    review_case.session.rollback()
    assert review_case.cms.writes == 0


def test_sqlite_replace_cannot_silently_rewrite_append_only_evidence(review_case):
    """SQLite REPLACE has implicit deletes and must obey the audit triggers."""
    proposed = review_case.ready()
    revision = review_case.session.get(Revision, proposed["revision_id"])
    evidence_id = revision.evidence_ids_json[0]
    sql = """INSERT OR REPLACE INTO evidence
        (id, site_id, source, source_type, content, observed_at, confidence,
         content_hash, owner, status, is_fixture, created_at)
        SELECT id, site_id, 'tampered-source', source_type, content, observed_at,
               confidence, content_hash, owner, status, is_fixture, created_at
        FROM evidence WHERE id = :id"""
    with pytest.raises(DBAPIError, match="append-only"):
        review_case.session.execute(text(sql), {"id": evidence_id})
    review_case.session.rollback()


def test_production_existing_page_requires_real_atomic_adapter(review_case):
    snapshot = review_case.cms.snapshot.model_copy(update={"metadata": {"atomic_compare_and_swap": False}})
    review_case.cms.snapshot = snapshot
    review_case.page.metadata_json = {"cms_snapshot": snapshot.model_dump(mode="json")}
    review_case.session.commit()
    proposed = review_case.ready()
    result = review_case.execute(proposed["revision_id"])
    assert result["status"] == "blocked"
    assert "production_adapter_requires_atomic_compare_and_swap" in result["details"]["reasons"]
    assert review_case.cms.writes == 0


@pytest.mark.parametrize("field", ["admin_token", "approval_token"])
def test_production_rejects_weak_high_authority_tokens(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", database_url="postgresql+psycopg://local/test",
                 api_token="operator-" + "x"*32, **{field: "x"})


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/", "https://[::1]/", "https://[::ffff:127.0.0.1]/",
    "https://169.254.169.254/latest/meta-data/", "https://user:pass@example.com/",
    "https://example.com:8443/", "https://example.com\\@127.0.0.1/",
])
def test_ssrf_disallowed_urls_fail_before_network(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_dns_answer_with_public_and_private_addresses_never_connects(monkeypatch):
    import socket
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(httpcore.ConnectError):
        PublicNetworkBackend().connect_tcp("example.com", 443)
    assert calls == []


def test_crawler_cross_origin_redirect_does_not_forward_request():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/"})
    crawler = Crawler("https://example.com", client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0)
    result = crawler.crawl_url("https://example.com/service")
    assert result.crawlable is None
    assert calls == ["https://example.com/robots.txt", "https://example.com/service"]


@pytest.fixture
def oauth_verifier(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    keyfile = tmp_path / "issuer-public.pem"
    keyfile.write_bytes(public)
    verifier = PinnedJWTVerifier("https://issuer.example", "https://seo.example/mcp", str(keyfile), {"approved-owner"})
    now = int(utcnow().timestamp())
    claims = {"iss": "https://issuer.example", "aud": "https://seo.example/mcp", "sub": "approved-owner",
              "iat": now-1, "exp": now+300, "scope": "seo:read", "client_id": "approved-client"}
    return verifier, private, claims


def test_oauth_valid_token_is_resource_and_subject_bound(oauth_verifier):
    verifier, key, claims = oauth_verifier
    result = asyncio.run(verifier.verify_token(jwt.encode(claims, key, algorithm="RS256")))
    assert result is not None
    assert result.scopes == ["seo:read"]
    assert result.resource == "https://seo.example/mcp"


@pytest.mark.parametrize("changes", [
    {"iss": "https://hostile.example"}, {"aud": "https://other.example/mcp"},
    {"sub": "unknown-owner"}, {"exp": 0}, {"scope": "seo:execute"},
    {"scope": ["seo:read"]}, {"iat": 32503680000},
])
def test_oauth_wrong_authority_or_expiry_is_rejected(oauth_verifier, changes):
    verifier, key, claims = oauth_verifier
    token = jwt.encode(claims | changes, key, algorithm="RS256")
    assert asyncio.run(verifier.verify_token(token)) is None


def test_oauth_hmac_confusion_and_unsigned_tokens_are_rejected(oauth_verifier):
    verifier, key, claims = oauth_verifier
    unsigned = jwt.encode(claims, "", algorithm="none")
    hmac_token = jwt.encode(claims, "not-the-public-key-" + "x"*32, algorithm="HS256")
    assert asyncio.run(verifier.verify_token(unsigned)) is None
    assert asyncio.run(verifier.verify_token(hmac_token)) is None


@pytest.mark.asyncio
async def test_remote_mcp_read_scope_cannot_mutate(monkeypatch, oauth_verifier):
    verifier, key, claims = oauth_verifier
    from mcp.server.auth.middleware import auth_context
    monkeypatch.setenv("MCP_OAUTH_ISSUER", verifier.issuer)
    monkeypatch.setenv("MCP_PUBLIC_URL", verifier.audience)
    monkeypatch.setenv("MCP_ALLOWED_SUBJECTS", "approved-owner")
    monkeypatch.setattr("seo_mcp.auth.PinnedJWTVerifier", lambda *args, **kwargs: verifier)
    monkeypatch.setenv("MCP_OAUTH_PUBLIC_KEY_FILE", "unused-by-test-double")
    token = await verifier.verify_token(jwt.encode(claims, key, algorithm="RS256"))
    monkeypatch.setattr(auth_context, "get_access_token", lambda: token)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})
    server = create_server(ControlClient(token="test-only", transport=httpx.MockTransport(handler)), remote=True)
    with pytest.raises(Exception, match="seo:propose"):
        await server.call_tool("create_task", {"site_id": str(uuid4()), "title": "Task", "objective": "Observe"})
    with pytest.raises(Exception, match="seo:execute"):
        await server.call_tool("execute_approved_revision", {"site_id": str(uuid4()), "revision_id": str(uuid4()), "idempotency_key": "test"})
    assert calls == []
