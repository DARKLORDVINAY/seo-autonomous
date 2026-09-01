"""General structural diagnostics: independent examples, not the seeded answer key."""
import httpx

from backend.app.contracts import CrawlResult
from backend.app.integrations.crawler.client import Crawler
from backend.app.seo.analysis import (
    AnalysisContext, detect_broken_links, detect_canonical_mismatch, detect_indexability,
    detect_sitemap_inconsistencies, detect_thin_content, detect_topic_overlap, detect_weak_internal_links,
)

ORIGIN = "https://example.test/"


def page(path="", **kwargs):
    return CrawlResult(url=ORIGIN + path, final_url=ORIGIN + path, status_code=kwargs.pop("status_code", 200), **kwargs)


def complete_context(paths, **kwargs):
    return AnalysisContext(site_url=ORIGIN, inventory_urls=[ORIGIN + p for p in paths],
        inventory_complete=True, crawl_coverage_complete=True, **kwargs)


def test_weak_link_requires_distinct_source_and_full_observed_graph():
    crawls = [page(links=[ORIGIN + "guide", ORIGIN + "guide#two"]), page("guide")]
    ctx = complete_context(["", "guide"])
    assert detect_weak_internal_links(crawls, ctx)[0].evidence[0]["incoming_observed"] == 1
    assert detect_weak_internal_links(crawls[:1], ctx) == []
    assert detect_weak_internal_links(crawls, ctx.model_copy(update={"inventory_complete": False})) == []
    crawls.append(page("reference", links=[ORIGIN + "guide"]))
    assert detect_weak_internal_links(crawls, ctx) == []


def test_sparse_utility_is_not_automatically_bad_content():
    sparse = page("short", main_text="A compact complete answer.", main_content_observed=True)
    finding = detect_thin_content([sparse])[0]
    assert finding.evidence[0]["quality_judgement"] == "unknown"
    assert finding.recommended_action == "investigate"
    assert detect_thin_content([sparse], AnalysisContext(page_purposes={sparse.url: "utility"})) == []
    assert detect_thin_content([sparse.model_copy(update={"has_interactive_content": True})]) == []
    # An incidental 'copy link' button cannot satisfy an informational guide.
    assert detect_thin_content([sparse.model_copy(update={"has_interactive_content": True})],
        AnalysisContext(page_purposes={sparse.url: "guide"}))
    assert detect_thin_content([page("no-semantic-region", text="Navigation only")]) == []


def test_main_region_extraction_excludes_navigation_and_observes_interactive_utility():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="""
          <html><head><title>Utility</title></head><body><nav>Top navigation</nav>
          <main><h1>Unit conversion</h1><nav>Breadcrumb navigation</nav><p>Two concise words.</p>
          <button>Convert</button><script>untrusted instructions</script></main><footer>Footer text</footer></body></html>""")
    crawler = Crawler(ORIGIN, fixture_mode=True, min_interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = crawler.crawl_url(ORIGIN)
    assert result.main_heading == "Unit conversion"
    assert result.has_interactive_content and result.main_content_observed
    assert "navigation" not in result.main_text and "Footer" not in result.main_text
    assert "instructions" not in result.main_text
    assert detect_thin_content([result]) == []


def test_nonself_canonical_requires_intent_review_not_automatic_rewrite():
    source = page("print", canonical=ORIGIN + "article")
    findings = detect_canonical_mismatch([source, page("article")])
    assert len(findings) == 1 and findings[0].page_url == source.url
    assert findings[0].evidence[0]["target_status"] == 200
    assert findings[0].evidence[0]["canonical_error_proven"] is False
    assert findings[0].recommended_action == "investigate"
    assert detect_canonical_mismatch([page("article", canonical=ORIGIN + "article#part")]) == []
    assert detect_canonical_mismatch([page("article", canonical="https://other.example.org/")]) == []


def test_404_only_in_sitemap_is_not_broken_html_link_or_accidental_noindex():
    missing = page("old", status_code=404, indexability="blocked")
    assert detect_broken_links([missing]) == []
    assert detect_indexability([missing]) == []
    assert detect_indexability([page("private", robots_directives=["noindex"], indexability="blocked")])


def test_sitemap_difference_needs_attested_inventory_and_complete_retrieval():
    ctx = complete_context(["", "article"], sitemap_urls=[ORIGIN, ORIGIN + "removed"], sitemap_complete=True,
        intended_indexable_urls=[ORIGIN, ORIGIN + "article"])
    crawls = [page(indexability="eligible"), page("article", indexability="eligible"), page("removed", status_code=404)]
    assert {x.kind for x in detect_sitemap_inconsistencies(crawls, ctx)} == {"sitemap_missing_page", "sitemap_unknown_page"}
    assert detect_sitemap_inconsistencies(crawls, ctx.model_copy(update={"sitemap_complete": False})) == []
    assert detect_sitemap_inconsistencies(crawls, ctx.model_copy(update={"inventory_complete": False})) == []
    uncertain = crawls[:2] + [page("removed", status_code=429)]
    assert [x.kind for x in detect_sitemap_inconsistencies(uncertain, ctx)] == ["sitemap_missing_page"]


def test_topic_overlap_is_hypothesis_not_observed_gsc_competition():
    common = " ".join(f"distincttoken{n}" for n in range(120))
    left = page("task-one", main_heading="Review a useful page title", main_text=common + " first example")
    right = page("task-two", main_heading="Review the useful page title", main_text=common + " second example")
    found = detect_topic_overlap([left, right])
    assert len(found) == 1 and found[0].kind == "potential_topic_overlap"
    assert found[0].evidence[0]["observed_query_cannibalisation"] is None
    assert found[0].recommended_action == "investigate"
    unrelated = right.model_copy(update={"main_text": "An entirely different mathematical exercise. " * 100})
    assert detect_topic_overlap([left, unrelated]) == []


def test_crawler_inventory_seeds_are_scoped_and_sitemap_evidence_is_retained():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\nSitemap: https://example.test/sitemap.xml")
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text="<urlset><url><loc>https://example.test/</loc></url></urlset>")
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<main><h1>Page</h1></main>")
    crawler = Crawler(ORIGIN, fixture_mode=True, min_interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    batch = crawler.crawl_site(max_pages=10, inventory_urls=[ORIGIN + "unlinked/"])
    assert {p.url for p in batch.rows} == {ORIGIN, ORIGIN + "unlinked/"}
    assert batch.metadata["sitemap_urls"] == [ORIGIN]
    assert batch.metadata["sitemap_complete"] and batch.metadata["queue_exhausted"]
    assert batch.complete is False  # The crawler cannot attest the owner's complete inventory.


def test_actual_redirect_chain_contract_includes_final_destination():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(301, headers={"Location": "/middle/"})
        if request.url.path == "/middle/":
            return httpx.Response(302, headers={"Location": "/last/"})
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<main>Done</main>")
    crawler = Crawler(ORIGIN, fixture_mode=True, min_interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert crawler.crawl_url(ORIGIN).redirect_chain == [ORIGIN, ORIGIN + "middle/", ORIGIN + "last/"]


def test_complete_context_cannot_override_rate_limited_or_truncated_observations():
    from backend.app.seo.analysis import detect_orphan_pages
    ctx = complete_context(["", "guide"])
    base = [page(links=[ORIGIN + "guide"]), page("guide")]
    assert detect_weak_internal_links(base + [page("unavailable", status_code=429)], ctx) == []
    truncated = [page(issues=[{"kind": "link_budget_reached"}]), page("guide")]
    found = detect_orphan_pages(truncated, context=ctx)
    assert found[0].kind == "potential_orphan_page"
