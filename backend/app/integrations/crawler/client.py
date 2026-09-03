"""Bounded deterministic same-origin crawler. Fetched HTML is never trusted policy."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from backend.app.contracts import CrawlResult
from backend.app.integrations.common import ObservationBatch, ProviderError, safe_client
from backend.app.integrations.crawler.network import PublicHTTPTransport, UnsafeURL, validate_url

USER_AGENT = "SpiralMaxSEO/0.1"


def _finite_json_number(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Non-finite numbers are not JSON observations")
    return value


def _schema_json(raw: str):
    value = json.loads(raw, parse_constant=_finite_json_number, parse_float=_finite_json_number)
    pending, visited = [(value, 0)], 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 10000:
            raise ValueError("Schema structure exceeds the bounded allowance")
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        if visited + len(pending) + len(children) > 10000:
            raise ValueError("Schema structure exceeds the bounded allowance")
        pending.extend((child, depth + 1) for child in children)
    return value


@dataclass
class RobotsState:
    parser: RobotFileParser | None
    status: str
    sitemaps: list[str]
    reason: str = ""


class Crawler:
    def __init__(self, site_url: str, *, client=None, fixture_mode=False,
                 max_bytes=2_000_000, max_redirects=5, min_interval=1.0, sleep=time.sleep,
                 max_total_bytes=20_000_000):
        if not 1024 <= max_bytes <= 5_000_000 or not 0 <= max_redirects <= 10:
            raise ValueError("Invalid crawler budget")
        if not max_bytes <= max_total_bytes <= 50_000_000:
            raise ValueError("Invalid total crawl byte budget")
        if not 0 <= min_interval <= 30:
            raise ValueError("Invalid crawler interval")
        # Injected clients are test-only or use our hardened transport. Never
        # allow a regular proxy-backed httpx client to silently skip DNS pinning.
        if client is not None and not isinstance(client._transport, (httpx.MockTransport, PublicHTTPTransport)):
            raise ValueError("Crawler requires hardened transport or injected MockTransport")
        if fixture_mode and (client is None or not isinstance(client._transport, httpx.MockTransport)):
            raise ValueError("Fixture mode requires an injected MockTransport")
        self.site_url = validate_url(site_url, fixture=fixture_mode)
        parts = urlsplit(self.site_url)
        self.origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if fixture_mode and parts.hostname != "example.test":
            raise ValueError("Fixture mode supports only reserved example.test")
        self.client = client or safe_client()
        self.is_fixture = fixture_mode
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.max_total_bytes, self.bytes_fetched = max_total_bytes, 0
        self.min_interval = min_interval
        self.sleep = sleep
        self.robots: RobotsState | None = None
        self._last_request = 0.0
        self.discovery_issues: list[dict] = []

    def _validate(self, url):
        return validate_url(url, origin=self.origin, fixture=self.is_fixture)

    def _fetch(self, url, *, enforce_robots=False):
        current = self._validate(url)
        redirects = []
        for _ in range(self.max_redirects + 1):
            if self.bytes_fetched >= self.max_total_bytes:
                raise ProviderError("Total crawl byte budget exhausted")
            if enforce_robots and not self._allowed(current):
                raise ProviderError("Redirect target disallowed or unknown under robots.txt")
            delay = self.min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                self.sleep(delay)
            try:
                self._last_request = time.monotonic()
                with self.client.stream("GET", current, follow_redirects=False,
                        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}, timeout=15) as response:
                    # No transparent compressed bombs; small crawler deliberately requests identity.
                    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise ProviderError("Compressed responses are unsupported by bounded crawler")
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ProviderError("Redirect has no Location")
                        try:
                            target = self._validate(urljoin(current, location))
                        except ValueError as exc:
                            raise UnsafeURL("Invalid redirect target") from exc
                        if target in redirects or target == current:
                            raise ProviderError("Redirect loop detected")
                        redirects.append(current)
                        current = target
                        continue
                    length = response.headers.get("content-length")
                    if length:
                        try:
                            if int(length) > self.max_bytes or int(length) < 0:
                                raise ProviderError("Response exceeds byte budget")
                        except ValueError as exc:
                            raise ProviderError("Invalid Content-Length") from exc
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        self.bytes_fetched += len(chunk)
                        if self.bytes_fetched > self.max_total_bytes:
                            raise ProviderError("Total crawl byte budget exhausted")
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise ProviderError("Response exceeds byte budget")
                    return response.status_code, dict(response.headers), bytes(body), current, redirects
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                # httpx prepares response.next_request even when redirects are
                # disabled. A hostile Location can fail there before our parser.
                raise ProviderError("Crawl network failure; state unknown") from exc
        raise ProviderError("Redirect budget exceeded")

    def inspect_robots(self) -> RobotsState:
        if self.robots is not None:
            return self.robots
        try:
            status, headers, body, final, _ = self._fetch(self.origin + "/robots.txt")
            parser = RobotFileParser()
            parser.set_url(final)
            if status in (404, 410):
                parser.parse([])
                self.robots = RobotsState(parser, "absent", [])
            elif status == 200:
                if "text/html" in headers.get("content-type", "").lower() or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                    self.robots = RobotsState(None, "unknown", [], "robots_returned_html")
                    return self.robots
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                self.robots = RobotsState(parser, "ok", parser.site_maps() or [])
                delay = parser.crawl_delay(USER_AGENT)
                if delay is not None:
                    if delay > 30:
                        self.robots = RobotsState(None, "unknown", [], "Crawl delay exceeds per-run budget")
                    else:
                        self.min_interval = max(self.min_interval, delay)
            else:
                self.robots = RobotsState(None, "unknown", [], f"robots_http_{status}")
        except (ProviderError, UnsafeURL):
            self.robots = RobotsState(None, "unknown", [], "robots_fetch_failed")
        return self.robots

    def _allowed(self, url):
        robots = self.inspect_robots()
        return bool(robots.parser is not None and robots.parser.can_fetch(USER_AGENT, url))

    def crawl_url(self, url: str) -> CrawlResult:
        url = self._validate(url)  # Reject malicious inputs before any robots/DNS access.
        robots = self.inspect_robots()
        if robots.parser is None:
            return CrawlResult(url=url, final_url=url, crawlable=None, issues=[{
                "kind": "robots_unknown", "detail": robots.reason, "severity": "warning"}])
        if not robots.parser.can_fetch(USER_AGENT, url):
            return CrawlResult(url=url, final_url=url, crawlable=False, indexability="unknown",
                issues=[{"kind": "robots_blocked", "severity": "warning",
                         "detail": "Disallowed crawling does not prove exclusion from Google index"}])
        try:
            status, headers, body, final, redirects = self._fetch(url, enforce_robots=True)
        except (ProviderError, UnsafeURL) as exc:
            return CrawlResult(url=url, final_url=url, crawlable=None, issues=[{
                "kind": "fetch_blocked", "detail": str(exc), "severity": "warning"}])
        result = CrawlResult(url=url, final_url=final, status_code=status,
                             redirect_chain=[*redirects, final] if redirects else [],
                             crawlable=True, content_hash=hashlib.sha256(body).hexdigest())
        if status != 200:
            result.indexability = "blocked" if status in (401, 403, 404, 410) else "unknown"
            result.issues.append({"kind": "http_status", "status_code": status, "severity": "warning"})
            return result
        content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in ("text/html", "application/xhtml+xml"):
            result.issues.append({"kind": "unsupported_content_type", "value": content_type, "severity": "info"})
            return result
        soup = BeautifulSoup(body, "html.parser")
        result.title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "description"})
        result.meta_description = str(description.get("content", "")) if description else ""
        base_node = soup.find("base", href=True)
        document_base = final
        if base_node:
            try:
                document_base = urljoin(final, str(base_node["href"]))
            except ValueError:
                result.issues.append({"kind": "invalid_base_url", "severity": "warning"})
        canonicals = soup.find_all("link", rel=lambda rel: rel and "canonical" in rel.lower().split())
        if canonicals:
            try:
                result.canonical = urljoin(document_base, str(canonicals[0].get("href", "")))
            except ValueError:
                result.issues.append({"kind": "invalid_canonical_url", "severity": "warning"})
        if len(canonicals) > 1:
            result.issues.append({"kind": "multiple_canonicals", "severity": "warning"})
        directives = [headers.get("x-robots-tag", "")]
        directives += [str(node.get("content", "")) for node in soup.find_all("meta")
                       if str(node.get("name", "")).lower() in ("robots", "googlebot")]
        result.robots_directives = sorted({item.strip().lower() for line in directives
                                           for item in line.split(",") if item.strip()})
        noindex = any("noindex" in item or item == "none" for item in result.robots_directives)
        result.indexability = "blocked" if noindex else "eligible"
        for anchor in soup.find_all("a", href=True):
            try:
                target = urljoin(document_base, anchor["href"])
            except ValueError:
                # One malformed hyperlink must not discard the valid page or
                # every subsequent link. Cross-origin links remain skipped.
                if not any(issue["kind"] == "invalid_link_url" for issue in result.issues):
                    result.issues.append({"kind": "invalid_link_url", "severity": "warning"})
                continue
            try:
                link = self._validate(target)
            except UnsafeURL:
                continue
            if link not in result.links:
                result.links.append(link)
            if len(result.links) >= 2000:
                result.issues.append({"kind": "link_budget_reached", "severity": "info"})
                break
        for node in soup.find_all("script", attrs={"type": "application/ld+json"})[:50]:
            try:
                result.schema.append(_schema_json(node.string or node.get_text()))
            except (ValueError, RecursionError):
                result.issues.append({"kind": "invalid_schema_json", "severity": "warning"})
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        result.text = soup.get_text(" ", strip=True)[:100000]
        # Avoid counting repeated navigation/footer boilerplate as useful page content.
        # If the publisher has no semantic content region, leave extraction unknown.
        region = soup.find("main") or soup.find("article")
        if region is not None:
            result.main_content_observed = True
            main = BeautifulSoup(str(region), "html.parser")
            for node in main(["nav", "footer", "header", "aside"]):
                node.decompose()
            heading = main.find("h1")
            result.main_heading = heading.get_text(" ", strip=True)[:1000] if heading else ""
            result.has_interactive_content = main.find(["form", "button", "input", "select", "textarea"]) is not None
            main_text = main.get_text(" ", strip=True)
            result.main_text = main_text[:100000]
            if len(main_text) > 100000:
                result.issues.append({"kind": "main_text_truncated", "severity": "info"})
        if not result.title:
            result.issues.append({"kind": "missing_title", "severity": "warning"})
        if not result.meta_description:
            result.issues.append({"kind": "missing_meta_description", "severity": "info"})
        # Eligible means only observed HTTP/meta conditions, never Google indexing proof.
        result.issues.append({"kind": "index_status_unverified", "severity": "info"})
        return result

    def discover_sitemap(self, *, max_urls=1000, max_sitemaps=20, max_depth=3) -> list[str]:
        if not 1 <= max_urls <= 10000 or not 1 <= max_sitemaps <= 50 or not 0 <= max_depth <= 5:
            raise ValueError("Invalid sitemap budget")
        robots = self.inspect_robots()
        if robots.parser is None:
            self.discovery_issues.append({"kind": "sitemap_skipped_robots_unknown"})
            return []
        pending = deque((url, 0) for url in (robots.sitemaps or [self.origin + "/sitemap.xml"]))
        visited, pages = set(), []
        while pending and len(visited) < max_sitemaps and len(pages) < max_urls:
            raw_url, depth = pending.popleft()
            try:
                url = self._validate(raw_url)
                if url in visited:
                    continue
                visited.add(url)
                status, _, body, _, _ = self._fetch(url, enforce_robots=True)
                if status != 200:
                    self.discovery_issues.append({"kind": "sitemap_http_status", "status": status})
                    continue
                root = ElementTree.fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
                tag = root.tag.rsplit("}", 1)[-1]
                if tag not in ("sitemapindex", "urlset"):
                    raise ValueError("Unsupported sitemap root")
                for child in root:
                    loc = next((node.text for node in child if node.tag.rsplit("}", 1)[-1] == "loc"), None)
                    if not loc:
                        continue
                    try:
                        target = self._validate(loc.strip())
                    except UnsafeURL:
                        self.discovery_issues.append({"kind": "unsafe_sitemap_url"})
                        continue
                    if tag == "sitemapindex":
                        if depth < max_depth:
                            pending.append((target, depth + 1))
                        else:
                            self.discovery_issues.append({"kind": "sitemap_depth_limit"})
                    elif target not in pages:
                        pages.append(target)
                        if len(pages) >= max_urls:
                            break
            except DefusedXmlException:
                self.discovery_issues.append({"kind": "unsafe_sitemap_xml"})
            except (ProviderError, UnsafeURL, ValueError, ElementTree.ParseError) as exc:
                self.discovery_issues.append({"kind": "sitemap_error", "error_type": type(exc).__name__})
        if pending or len(pages) >= max_urls:
            self.discovery_issues.append({"kind": "sitemap_budget_reached"})
        return pages

    def crawl_site(self, *, max_pages=100, inventory_urls: list[str] | None = None) -> ObservationBatch[CrawlResult]:
        if not 1 <= max_pages <= 1000:
            raise ValueError("Crawl page budget must be 1..1000")
        if inventory_urls is not None and len(inventory_urls) > 1000:
            raise ValueError("Inventory crawl seed budget must be at most 1000 URLs")
        # Inventory is only a discovery input. Coverage/authority is attested separately
        # by the owning application against its versioned release manifest.
        inventory = [self._validate(url) for url in (inventory_urls or [])]
        sitemap_urls = self.discover_sitemap(max_urls=max_pages)
        sitemap_complete = not self.discovery_issues
        pending = deque(dict.fromkeys([self.site_url, *inventory, *sitemap_urls]))
        queued = set(pending)
        visited, results = set(), []
        queue_budget = max_pages * 5
        while pending and len(results) < max_pages:
            url = pending.popleft()
            if url in visited:
                continue
            visited.add(url)
            result = self.crawl_url(url)
            results.append(result)
            for link in result.links:
                if link not in queued and len(queued) < queue_budget:
                    pending.append(link)
                    queued.add(link)
        flags = ["bounded_same_origin_sample", "google_index_status_unknown"]
        if pending:
            flags.append("page_budget_reached")
        if self.discovery_issues:
            flags.append("sitemap_discovery_incomplete")
        if self.is_fixture:
            flags.append("fixture_data")
        return ObservationBatch(results, "fixture:crawler" if self.is_fixture else "crawler", flags,
            complete=False, metadata={"origin": self.origin, "robots_status": self.inspect_robots().status,
                "sitemap_urls": sitemap_urls, "sitemap_complete": sitemap_complete,
                "queue_exhausted": not pending,
                "discovery_issues": self.discovery_issues, "fetched_pages": len(results),
                "bytes_fetched": self.bytes_fetched, "max_total_bytes": self.max_total_bytes})
