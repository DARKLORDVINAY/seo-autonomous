"""General structural diagnostics: independent examples, not the seeded answer key."""
import httpx

from backend.app.contracts import CrawlResult
from backend.app.integrations.crawler.client import Crawler
from backend.app.seo.analysis import (
    AnalysisContext, detect_broken_links, detect_canonical_cycles, detect_canonical_mismatch,
    detect_indexability, detect_orphan_pages, detect_sitemap_inconsistencies, detect_soft_404,
    detect_thin_content, detect_topic_overlap, detect_weak_internal_links,
)

ORIGIN = "https://example.test/"


def page(path="", **kwargs):
    url = ORIGIN + path
    return CrawlResult(url=url, final_url=kwargs.pop("final_url", url), status_code=kwargs.pop("status_code", 200), **kwargs)


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
    external = detect_canonical_mismatch([page("article", canonical="https://other.example.org/")])
    assert external[0].evidence[0]["cross_host_canonical"] is True


def test_trusted_indexing_intent_suppresses_deliberate_exclusions_but_not_conflicts():
    public = page("public", indexability="blocked", robots_directives=["noindex"])
    private = page("private", indexability="blocked", robots_directives=["noindex"])
    ctx = AnalysisContext(site_url=ORIGIN, intended_indexable_urls=[public.url])
    assert [item.page_url for item in detect_indexability([public, private], ctx)] == [public.url]
    blocked = page("robots", status_code=None, crawlable=False, issues=[{"kind": "robots_blocked"}])
    finding = detect_indexability([blocked], ctx.model_copy(update={"intended_indexable_urls": [blocked.url]}))[0]
    assert finding.evidence[0]["robots_blocked"] is True


def test_canonical_cycle_is_one_grouped_finding_and_not_duplicate_mismatches():
    first = page("one", canonical=ORIGIN + "two")
    second = page("two", canonical=ORIGIN + "three")
    third = page("three", canonical=ORIGIN + "one")
    crawls = [third, first, second]
    cycles = detect_canonical_cycles(crawls)
    assert len(cycles) == 1 and cycles[0].kind == "canonical_cycle"
    assert set(cycles[0].evidence[0]["pages"]) == {first.url, second.url, third.url}
    assert detect_canonical_mismatch(crawls) == []


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
    left = page("task-one", main_heading="Review a useful page title", main_text=common + " first example", main_content_observed=True)
    right = page("task-two", main_heading="Review the useful page title", main_text=common + " second example", main_content_observed=True)
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
    ctx = complete_context(["", "guide"])
    base = [page(links=[ORIGIN + "guide"]), page("guide")]
    assert detect_weak_internal_links([base[0], page("guide", status_code=429)], ctx) == []
    assert detect_weak_internal_links(base + [page("out-of-scope", status_code=429)], ctx)
    truncated = [page(issues=[{"kind": "link_budget_reached"}]), page("guide")]
    found = detect_orphan_pages(truncated, context=ctx)
    assert found[0].kind == "potential_orphan_page"


def test_broken_internal_link_preserves_source_destination_identity():
    source = page("guide", links=[ORIGIN + "gone"])
    missing = page("gone", status_code=410)
    finding = detect_broken_links([source, missing])[0]
    assert finding.kind == "broken_internal_link" and finding.page_url == source.url
    assert finding.evidence[0]["source_url"] == source.url
    assert finding.evidence[0]["target_url"] == missing.url


def test_soft_404_requires_missing_template_heading_and_body_not_article_topic():
    missing = page("missing", title="Page not found", main_heading="Page not found",
        main_text="We could not find the requested document. Return to the index.",
        main_content_observed=True, indexability="eligible")
    found = detect_soft_404([missing], AnalysisContext(intended_indexable_urls=[missing.url]))
    assert len(found) == 1 and found[0].kind == "soft_404"
    article = page("what-is-404", title="What is a 404 page?", main_heading="What is a 404 page?",
        main_text="A 404 response says that a requested page was not found. This article explains the protocol.",
        main_content_observed=True, indexability="eligible")
    assert detect_soft_404([article]) == []


def test_unobserved_rendered_content_is_not_called_thin():
    loading = page("app", main_text="Loading the educational exercise.",
        main_content_observed=False, has_interactive_content=True)
    assert detect_thin_content([loading], AnalysisContext(page_purposes={loading.url: "educational_article"})) == []


def test_trusted_concise_and_small_collection_purposes_prevent_proxy_alerts():
    definition = page("definition", main_text="A concise, complete definition.", main_content_observed=True)
    assert detect_thin_content([definition], AnalysisContext(page_purposes={definition.url: "concise_dictionary_definition"})) == []
    crawls = [page(links=[ORIGIN + "one"]), page("one", links=[ORIGIN])]
    ctx = complete_context(["", "one"], page_purposes={ORIGIN + "one": "single_exercise_in_small_collection"})
    assert detect_weak_internal_links(crawls, ctx) == []


def test_intentionally_private_inventory_does_not_downgrade_public_graph_evidence():
    private = page("private", status_code=None, crawlable=False, indexability="unknown")
    public = page("public")
    crawls = [page(), public, private]
    ctx = complete_context(["", "public", "private"], intended_indexable_urls=[ORIGIN, public.url])
    orphan = detect_orphan_pages(crawls, context=ctx)
    assert len(orphan) == 1 and orphan[0].page_url == public.url and orphan[0].kind == "orphan_page"
    linked = [page(links=[public.url]), public.model_copy(update={"links": [ORIGIN]}), private]
    assert detect_weak_internal_links(linked, ctx)[0].page_url == public.url


def test_redirect_alias_body_is_not_independently_judged_as_thin_or_soft_404():
    target = ORIGIN + "target"
    alias = page("old", final_url=target, redirect_chain=[ORIGIN + "old", target],
        title="Page not found", main_heading="Page not found",
        main_text="We could not find the requested document.", main_content_observed=True)
    ctx = AnalysisContext(intended_indexable_urls=[target], page_purposes={target: "educational_article"})
    assert detect_thin_content([alias], ctx) == []
    assert detect_soft_404([alias], ctx) == []
    graph_ctx = complete_context(["", "target", "old"], intended_indexable_urls=[ORIGIN, target])
    assert detect_weak_internal_links([page(links=[target]), page("target", links=[ORIGIN]), alias], graph_ctx) == []
