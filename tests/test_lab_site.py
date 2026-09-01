"""Independent checks of the lab's authored conditions and client consent gates.

These tests inspect the generated release. They do not feed ground-truth labels to
the crawler, opportunity engine, or agents. Browser/host verification is separate.
"""

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
import pytest

from test_lab import build


BASE = "https://spiral-max-seo-test-lab.pages.dev"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    output = tmp_path_factory.mktemp("lab-release")
    inventory = build.build_site(BASE, output)
    documents = {
        page["path"]: BeautifulSoup((output / page["path"].strip("/") / "index.html").read_text(), "html.parser")
        for page in inventory["pages"]
    }
    return output, inventory, documents


def test_release_is_26_declared_demonstration_pages_and_inventory_has_no_seed_labels(release):
    output, inventory, documents = release
    assert len(documents) == 26
    assert len(inventory["pages"]) == 26
    assert set(inventory) == {"schema_version", "base_url", "pages"}
    assert inventory["base_url"] == BASE
    for entry in inventory["pages"]:
        assert set(entry) == {"path", "content_sha256", "desired_indexing", "purpose"}
        assert entry["desired_indexing"] == "index"
        body = (output / entry["path"].strip("/") / "index.html").read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["content_sha256"]
        soup = documents[entry["path"]]
        assert soup.html["lang"] == "en"
        assert "Demonstration / test project" in soup.select_one(".disclosure").get_text()
        assert len(soup.select("main article h1")) == 1
        assert soup.select_one('a[href="#main-content"]')
        assert soup.select_one('meta[name="lab-page-purpose"]')["content"] == entry["purpose"]
        assert not soup.select('script[type="application/ld+json"]')
    assert len(list(output.rglob("*.html"))) == 27  # The custom 404 is outside the page inventory.
    assert not (output / "ground_truth.json").exists()
    assert not (output / "pages.json").exists()


def test_link_graph_has_exact_orphans_one_weak_page_and_one_broken_destination(release):
    _, _, documents = release
    incoming = {path: set() for path in documents}
    missing = defaultdict(set)
    for source, soup in documents.items():
        for anchor in soup.select("a[href]"):
            link = urlsplit(anchor["href"])
            if link.netloc or not link.path or link.path == source:
                continue
            if link.path in incoming:
                incoming[link.path].add(source)
            else:
                missing[link.path].add(source)
    assert {path for path, sources in incoming.items() if not sources} == {
        "/reference/response-headers/", "/reference/link-labels/",
    }
    assert {path for path, sources in incoming.items() if len(sources) == 1} == {"/guides/linked-resources/"}
    assert incoming["/guides/linked-resources/"] == {"/guides/"}
    assert len(incoming["/release-notes/"]) >= 2
    assert dict(missing) == {"/reference/missing-response-example/": {"/guides/status-codes/"}}


def test_metadata_and_indexing_conditions_have_exact_authored_scope(release):
    _, _, documents = release
    titles = defaultdict(set)
    descriptions = defaultdict(set)
    mismatches = {}
    noindex = set()
    for path, soup in documents.items():
        titles[soup.title.get_text()].add(path)
        descriptions[soup.select_one('meta[name="description"]')["content"]].add(path)
        canonicals = soup.select('link[rel="canonical"]')
        assert len(canonicals) == 1
        canonical = canonicals[0]["href"]
        if canonical != BASE + path:
            mismatches[path] = canonical
        if "noindex" in soup.select_one('meta[name="robots"]')["content"]:
            noindex.add(path)
    pair = {"/guides/page-checklist/", "/guides/preflight-checklist/"}
    assert [paths for paths in titles.values() if len(paths) > 1] == [pair]
    assert [paths for paths in descriptions.values() if len(paths) > 1] == [pair]
    assert documents["/guides/page-checklist/"].article.get_text() != documents["/guides/preflight-checklist/"].article.get_text()
    assert mismatches == {"/guides/url-preferences/": BASE + "/guides/canonical-urls/"}
    assert noindex == {"/guides/crawl-eligibility/"}


def test_sitemap_diff_and_missing_page_contract_are_explicit(release):
    output, _, documents = release
    sitemap = ElementTree.parse(output / "sitemap.xml")
    listed = {urlsplit(item.text).path for item in sitemap.findall("{*}url/{*}loc")}
    assert set(documents) - listed == {"/release-notes/"}
    assert listed - set(documents) == {"/reference/missing-sitemap-example/"}
    assert {"/reference/response-headers/", "/reference/link-labels/"} <= listed
    assert all(item.text.startswith(BASE + "/") for item in sitemap.findall("{*}url/{*}loc"))
    assert "Allow: /" in (output / "robots.txt").read_text()
    assert not (output / "_redirects").exists()  # Never add an SPA catch-all that turns missing addresses into 200s.
    assert not (output / "reference/missing-response-example/index.html").exists()
    assert not (output / "reference/missing-sitemap-example/index.html").exists()
    error_page = BeautifulSoup((output / "404.html").read_text(), "html.parser")
    assert "noindex" in error_page.select_one('meta[name="robots"]')["content"]
    assert "Demonstration / test project" in error_page.get_text()
    assert "That page is not here" in error_page.h1.get_text()


def test_main_content_distinguishes_incomplete_notes_from_useful_short_utilities(release):
    _, inventory, documents = release
    purposes = {entry["path"]: entry["purpose"] for entry in inventory["pages"]}
    counts = {path: len(re.findall(r"\b\w+\b", soup.article.get_text(" ", strip=True))) for path, soup in documents.items()}
    informational = {path for path, purpose in purposes.items() if purpose in {"guide", "note", "reference"}}
    assert {path for path in informational if counts[path] < 80} == {
        "/notes/navigation-draft/", "/notes/metadata-draft/",
    }
    assert all(counts[path] >= 140 for path in informational if not path.startswith("/notes/"))
    assert counts["/privacy/"] < 80
    assert purposes["/privacy/"] == "utility"
    assert len(documents["/exercises/"].select('input[type="checkbox"][name="lab-step"]')) == 3
    assert documents["/exercises/"].select_one("#complete-checklist").has_attr("disabled")
    first = documents["/exercises/title-review/"]
    second = documents["/exercises/title-audit/"]
    assert first.title.get_text() != second.title.get_text()
    assert first.select_one('meta[name="description"]')["content"] != second.select_one('meta[name="description"]')["content"]
    # The same actual task is present in both bodies; this is potential overlap,
    # not synthetic evidence of queries competing in Google's results.
    a = set(re.findall(r"\w+", first.article.get_text().lower()))
    b = set(re.findall(r"\w+", second.article.get_text().lower()))
    assert len(a & b) / len(a | b) > 0.90


def test_builder_does_not_read_or_publish_evaluator_inputs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy(ROOT / "test_lab/pages.json", source / "pages.json")
    shutil.copytree(ROOT / "test_lab/assets", source / "assets")
    (source / "ground_truth.json").write_text("DO-NOT-PUBLISH-INDEPENDENT-EVALUATOR-CANARY")
    monkeypatch.setattr(build, "SOURCE", source)
    target = tmp_path / "release"
    build.build_site(BASE, target)
    assert not (target / "ground_truth.json").exists()
    assert all(b"INDEPENDENT-EVALUATOR-CANARY" not in path.read_bytes() for path in target.rglob("*") if path.is_file())
    (source / "ground_truth.json").unlink()
    build.build_site(BASE, tmp_path / "without-ground-truth")
    assert (target / "index.html").read_bytes() == (tmp_path / "without-ground-truth/index.html").read_bytes()


@pytest.mark.parametrize("url", [
    "http://public.example.com", "https://user:secret@public.example.com", "https://public.example.com/path/",
    "https://public.example.com/?token=secret", "https://public.example.com/#fragment", "https://localhost",
    "https://example.test", "https://127.0.0.1", "https://[::1]", "https://public.example.com:8000",
    "https://public.example.com\\@evil.example.com", "https://public.example.com\n", "https://bad_host.example.com",
    "https://public.example.com.", "https://public.example.com%2fevil", "https://-bad.example.com",
])
def test_public_origin_validation_rejects_ambiguous_or_private_targets(url):
    with pytest.raises(ValueError):
        build.validate_base_url(url)


def test_explicit_fixture_origin_is_narrow_and_cannot_load_analytics(tmp_path):
    with pytest.raises(ValueError):
        build.build_site("https://example.test", tmp_path / "implicit")
    result = build.build_site("https://example.test", tmp_path / "fixture", fixture=True)
    assert result["base_url"] == "https://example.test"
    with pytest.raises(ValueError):
        build.build_site(BASE, tmp_path / "public-labelled-fixture", fixture=True)
    with pytest.raises(ValueError):
        build.build_site("https://example.test", tmp_path / "fixture-analytics", fixture=True, measurement_id="G-TEST123456")
    with pytest.raises(ValueError):
        build.build_site("https://example.test", tmp_path / "fixture-verification", fixture=True,
                         verification_token="test_verification_token_123456")


@pytest.mark.parametrize("path", ["../private/", "//evil.example/", "/../private/", "/%2e%2e/private/", "/a\\b/", "javascript:alert(1)"])
def test_page_paths_cannot_escape_output_or_create_active_urls(path):
    with pytest.raises(ValueError):
        build.validate_path(path)


def test_builder_refuses_to_overwrite_source_or_nonempty_release(tmp_path):
    with pytest.raises(ValueError):
        build.build_site(BASE, ROOT / "test_lab")
    with pytest.raises(ValueError):
        build.build_site(BASE, ROOT)
    (tmp_path / "existing.txt").write_text("preserve this")
    with pytest.raises(ValueError):
        build.build_site(BASE, tmp_path)
    assert (tmp_path / "existing.txt").read_text() == "preserve this"


def test_content_is_escaped_and_public_tag_inputs_cannot_inject_markup(tmp_path):
    malicious = '<script src="https://evil.example/x.js">execute()</script>'
    page = {
        "path": "/", "title": malicious, "description": '\"/><script>alert(1)</script>',
        "purpose": "home", "heading": malicious,
        "sections": [{"heading": malicious, "paragraphs": [malicious], "items": [malicious]}],
        "links": [{"path": "/guides/", "label": malicious}],
    }
    soup = BeautifulSoup(build.render_page(page, base_url=BASE, measurement_id="", verification_token=""), "html.parser")
    assert soup.title.get_text() == malicious
    assert soup.h1.get_text() == malicious
    assert [script.get("src") for script in soup.select("script")] == ["/assets/site.js"]
    for bad_id in ['G-TEST123456\" onload="evil()', "G-1", "G-test123456", "<script>"]:
        with pytest.raises(ValueError):
            build.build_site(BASE, tmp_path / "bad-id", measurement_id=bad_id)
    with pytest.raises(ValueError):
        build.build_site(BASE, tmp_path / "bad-token", verification_token='abc\"><script>alert(1)</script>')


def test_analytics_is_absent_by_default_and_public_tags_do_not_load_remote_code(release, tmp_path):
    output, _, documents = release
    for soup in documents.values():
        assert soup.select_one('meta[name="lab-ga4-measurement-id"]') is None
        assert soup.select_one('meta[name="google-site-verification"]') is None
        assert not soup.select_one("#analytics-allow")
        assert all(not urlsplit(script.get("src", "")).netloc for script in soup.select("script"))
    assert "google-analytics.com" not in (output / "_headers").read_text()
    configured = tmp_path / "configured"
    build.build_site(BASE, configured, measurement_id="G-TEST123456", verification_token="public_verification_token_123456")
    soup = BeautifulSoup((configured / "exercises/index.html").read_text(), "html.parser")
    assert soup.select_one('meta[name="lab-ga4-measurement-id"]')["content"] == "G-TEST123456"
    assert soup.select_one('meta[name="google-site-verification"]')["content"] == "public_verification_token_123456"
    assert soup.select_one("#analytics-allow")
    assert len(soup.select("script")) == 1 and soup.script["src"] == "/assets/site.js"
    headers = (configured / "_headers").read_text()
    assert "https://www.googletagmanager.com" in headers
    assert "'unsafe-inline'" not in headers and "'unsafe-eval'" not in headers
    assert "form-action 'none'" in headers and "Referrer-Policy: no-referrer" in headers


CLIENT_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = JSON.parse(process.argv[2]);
class Element {
  constructor() { this.disabled = false; this.checked = false; this.listeners = {}; this.textContent = ''; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  emit(name) { for (const callback of this.listeners[name] || []) callback(); }
}
const ids = Object.fromEntries(['analytics-allow', 'analytics-decline', 'analytics-status',
  'complete-checklist', 'exercise-result'].map(id => [id, new Element()]));
const checks = [new Element(), new Element(), new Element()];
const scripts = [];
const document = {
  referrer: 'https://www.google.com/search?q=private-query#private',
  querySelector: () => scenario.id ? {content: scenario.id} : null,
  querySelectorAll: () => checks,
  getElementById: id => ids[id],
  createElement: () => new Element(),
  head: {appendChild: element => scripts.push(element)}
};
const window = {location: {origin: 'https://public.example.com', pathname: '/exercises/',
  search: '?email=private@example.com', hash: '#private'}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document, window, URL, Date});
const initialDisabled = ids['complete-checklist'].disabled;
ids['complete-checklist'].emit('click');
if (scenario.before === 'allow') ids['analytics-allow'].emit('click');
if (scenario.before === 'decline') ids['analytics-decline'].emit('click');
checks[0].checked = true; checks[0].emit('change');
const oneCheckDisabled = ids['complete-checklist'].disabled;
checks[1].checked = true; checks[1].emit('change');
checks[2].checked = true; checks[2].emit('change');
const ready = !ids['complete-checklist'].disabled;
ids['complete-checklist'].emit('click');
ids['complete-checklist'].emit('click');
if (scenario.after === 'allow') ids['analytics-allow'].emit('click');
ids['complete-checklist'].emit('click');
if (scenario.loadError && scripts[0]) scripts[0].emit('error');
console.log(JSON.stringify({initialDisabled, oneCheckDisabled, ready,
  completedDisabled: ids['complete-checklist'].disabled,
  checksDisabled: checks.every(check => check.disabled),
  scriptUrls: scripts.map(script => script.src),
  calls: (window.dataLayer || []).map(args => Array.from(args)),
  result: ids['exercise-result'].textContent, status: ids['analytics-status'].textContent}));
"""


def run_client(scenario):
    node = os.environ.get("CODEX_PRIMARY_RUNTIME_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node is required for the isolated JavaScript consent/gating checks")
    result = subprocess.run(
        [node, "-e", CLIENT_HARNESS, str(ROOT / "test_lab/assets/site.js"), json.dumps(scenario)],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("scenario", [
    {"id": ""}, {"id": "G-TEST123456"}, {"id": "G-TEST123456", "before": "decline"},
    {"id": "INVALID", "before": "allow"},
])
def test_checklist_works_locally_without_analytics_consent(scenario):
    result = run_client(scenario)
    assert result["initialDisabled"] and result["oneCheckDisabled"] and result["ready"]
    assert result["completedDisabled"] and result["checksDisabled"]
    assert result["calls"] == [] and result["scriptUrls"] == []
    assert "no analytics event was sent" in result["result"]


def test_explicit_consent_queues_one_test_completion_with_no_sensitive_url_components():
    result = run_client({"id": "G-TEST123456", "before": "allow"})
    events = [call for call in result["calls"] if call[0] == "event"]
    assert [call[1] for call in events] == ["page_view", "lab_checklist_complete"]
    assert result["scriptUrls"] == ["https://www.googletagmanager.com/gtag/js?id=G-TEST123456"]
    assert events[1][2] == {"lab_mode": True, "lab_exercise": "page-review"}
    assert events[0][2] == {"page_location": "https://public.example.com/exercises/", "page_referrer": "https://www.google.com/"}
    config = next(call[2] for call in result["calls"] if call[0] == "config")
    assert config["send_page_view"] is False and config["allow_google_signals"] is False
    assert config["allow_ad_personalization_signals"] is False
    assert "private" not in json.dumps(result["calls"])
    assert "receipt by analytics has not been verified" in result["result"]


def test_consent_after_completion_does_not_retroactively_emit_a_conversion():
    result = run_client({"id": "G-TEST123456", "after": "allow"})
    events = [call[1] for call in result["calls"] if call[0] == "event"]
    assert events == ["page_view"]
    assert "no analytics event was sent" in result["result"]


def test_analytics_load_failure_preserves_local_practice_and_is_not_claimed_as_delivery():
    result = run_client({"id": "G-TEST123456", "before": "allow", "loadError": True})
    assert result["completedDisabled"]
    assert "could not load" in result["status"]
    assert "receipt by analytics has not been verified" in result["result"]
