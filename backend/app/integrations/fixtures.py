"""Deterministic labelled offline providers. Reserved hosts never reach network."""
from __future__ import annotations

import threading
from datetime import date, timedelta
from html import escape

import httpx

from backend.app.contracts import CMSPage, ConcurrencyConflict, GA4Row, GSCRow
from backend.app.integrations.common import ObservationBatch
from backend.app.integrations.crawler.client import Crawler
from backend.app.integrations.wordpress.client import bounded_changes


def fixture_pages() -> list[CMSPage]:
    return [
        CMSPage(external_id="pages:1", url="https://example.test/", title="Example Window Cleaning",
            content='<h1>Example Window Cleaning</h1><p>Fixture business only.</p><a href="/windows/">Window cleaning</a>',
            meta_description="Demonstration site, never a real business.", slug="", metadata={"provider": "fixture"}),
        CMSPage(external_id="pages:2", url="https://example.test/windows/", title="Windows",
            content='<h1>Residential window cleaning</h1><p>Example service information for the offline test site.</p><a href="/missing/">Contact</a>',
            meta_description="", slug="windows", metadata={"provider": "fixture"}),
        CMSPage(external_id="pages:3", url="https://example.test/gutters/", title="Gutter cleaning",
            content="<h1>Gutter cleaning</h1><p>Fixture-only gutter service page.</p>",
            meta_description="Demonstration gutter cleaning service.", slug="gutters", metadata={"provider": "fixture"}),
    ]


class FixtureCMS:
    is_fixture = True
    supports_atomic_updates = True

    def __init__(self, pages: list[CMSPage] | None = None):
        self._pages = {page.external_id: page.model_copy(deep=True) for page in (fixture_pages() if pages is None else pages)}
        self._lock = threading.RLock()

    def get_page(self, external_id: str) -> CMSPage:
        with self._lock:
            if external_id not in self._pages:
                raise KeyError("Fixture CMS page not found")
            return self._pages[external_id].model_copy(deep=True)

    def list_pages(self) -> list[CMSPage]:
        with self._lock:
            return [page.model_copy(deep=True) for page in self._pages.values()]

    def update_page(self, external_id: str, changes: dict, *, expected_fingerprint: str) -> CMSPage:
        changes = bounded_changes(changes)
        with self._lock:
            page = self.get_page(external_id)
            if not expected_fingerprint or page.fingerprint != expected_fingerprint:
                raise ConcurrencyConflict("Fixture page changed after proposal")
            updated = CMSPage.model_validate(page.model_dump() | changes)
            self._pages[external_id] = updated
            return updated.model_copy(deep=True)

    def create_draft(self, title: str, content: str) -> CMSPage:
        bounded_changes({"title": title, "content": content})
        with self._lock:
            identifier = max([int(key.split(":")[-1]) for key in self._pages] + [0]) + 1
            page = CMSPage(external_id=f"pages:{identifier}", url=f"https://example.test/?page_id={identifier}",
                title=title, content=content, status="draft", metadata={"provider": "fixture"})
            self._pages[page.external_id] = page
            return page.model_copy(deep=True)


def fixture_crawler(pages: list[CMSPage] | None = None) -> Crawler:
    pages = fixture_pages() if pages is None else pages
    by_url = {page.url: page for page in pages}

    def handler(request):
        if request.url.host != "example.test":
            raise AssertionError("Fixture never accesses another host")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\nSitemap: https://example.test/sitemap.xml\n")
        if request.url.path == "/sitemap.xml":
            xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
                f"<url><loc>{escape(page.url)}</loc></url>" for page in pages) + "</urlset>"
            return httpx.Response(200, text=xml, headers={"Content-Type": "application/xml"})
        page = by_url.get(str(request.url))
        if page is None:
            return httpx.Response(404, text="Not found", headers={"Content-Type": "text/html"})
        html = f'<html><head><title>{escape(page.title)}</title><meta name="description" content="{escape(page.meta_description)}"><link rel="canonical" href="{escape(page.url)}"></head><body>{page.content}</body></html>'
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    return Crawler("https://example.test/", client=httpx.Client(transport=httpx.MockTransport(handler)),
                   fixture_mode=True, min_interval=0)


def fixture_observations(*, end: date | None = None) -> dict:
    end = end or date(2026, 8, 28)
    gsc, ga4 = [], []
    for day in range(56):
        observed_date = end - timedelta(days=55 - day)
        decay = day >= 28
        for page, query, impressions, old_clicks, new_clicks in (
            ("https://example.test/windows/", "window cleaning", 1000, 90, 35),
            ("https://example.test/gutters/", "gutter cleaning", 300, 20, 20),
        ):
            clicks = new_clicks if decay else old_clicks
            gsc.append(GSCRow(date=observed_date, page=page, query=query,
                clicks=clicks, impressions=impressions, position=8 if "windows" in page else 6, data_state="final"))
            ga4.append(GA4Row(date=observed_date, landing_page=page, sessions=clicks,
                key_events=3 if not decay else 1, qualified_conversions=None,
                conversion_value=None, quality_flags=["fixture_data", "business_conversion_definition_unconfirmed"]))
    return {
        "gsc": ObservationBatch(gsc, "fixture:gsc", ["fixture_data", "gsc_top_rows_only"], False,
                                {"data_state": "final", "business": "fictional", "full_dataset_guaranteed": False}),
        "ga4": ObservationBatch(ga4, "fixture:ga4", ["fixture_data", "business_conversion_definition_unconfirmed"], False,
                                {"qualified_conversion_semantics_verified": False}),
        "crawl": fixture_crawler().crawl_site(max_pages=10),
    }
