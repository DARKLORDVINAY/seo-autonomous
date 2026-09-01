"""Bounded authenticated WordPress reads/drafts with explicit concurrency limits.

Core WordPress has no atomic fingerprint precondition. Existing-page writes are
therefore OFF by default. An operator can opt into optimistic writes for a
reviewed deployment with controlled editorial access; never call this atomic.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from backend.app.contracts import CMSPage, ConcurrencyConflict, ProviderUnavailable
from backend.app.integrations.common import AmbiguousWriteError, MalformedResponse, ProviderError, json_response, request, safe_client
from backend.app.integrations.crawler.network import validate_url


def bounded_changes(changes: dict[str, Any]) -> dict[str, str]:
    if not changes or set(changes) - {"title", "content", "meta_description"}:
        raise ValueError("Only title, content, and registered meta description updates are supported")
    limits = {"title": 300, "content": 200000, "meta_description": 500}
    for field, value in changes.items():
        if not isinstance(value, str) or len(value) > limits[field] or "\x00" in value:
            raise ValueError(f"Invalid or oversized {field}")
    return dict(changes)


class WordPressClient:
    is_fixture = False
    supports_atomic_updates = False

    def __init__(self, base_url: str, username: str, application_password: str, *,
                 client=None, meta_description_key: str | None = None,
                 allow_optimistic_writes: bool = False, max_pages: int = 10000):
        if not base_url or not username or not application_password:
            raise ProviderUnavailable("WORDPRESS_URL, WORDPRESS_USERNAME, and WORDPRESS_APPLICATION_PASSWORD are required")
        self.base_url = validate_url(base_url).rstrip("/")
        if "?" in self.base_url or "#" in self.base_url:
            raise ValueError("WordPress base URL must not contain query or fragment")
        if meta_description_key and not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,100}", meta_description_key):
            raise ValueError("Invalid registered meta description key")
        if not 1 <= max_pages <= 10000:
            raise ValueError("Invalid CMS inventory budget")
        self.client = client or safe_client()
        self.auth = httpx.BasicAuth(username, application_password)
        self.meta_description_key = meta_description_key
        self.allow_optimistic_writes = allow_optimistic_writes
        self.max_pages = max_pages

    def _url(self, path: str) -> str:
        return f"{self.base_url}/wp-json/wp/v2/{path}"

    @staticmethod
    def _identity(external_id: str) -> tuple[str, int]:
        match = re.fullmatch(r"(pages|posts):([1-9][0-9]{0,14})", external_id)
        if not match:
            raise ValueError("CMS identifier must be pages:<id> or posts:<id>")
        return match.group(1), int(match.group(2))

    def _page(self, data: Any, collection: str) -> CMSPage:
        try:
            if not isinstance(data, dict) or not isinstance(data["id"], int) or data["id"] <= 0:
                raise ValueError("Invalid post ID")
            for field in ("title", "content"):
                if not isinstance(data[field], dict) or not isinstance(data[field].get("raw"), str):
                    raise ValueError("Authenticated edit context with raw fields is required")
            meta = data.get("meta", {})
            if not isinstance(meta, dict):
                raise ValueError("Invalid metadata")
            description = meta.get(self.meta_description_key, "") if self.meta_description_key else ""
            if not isinstance(description, str):
                raise ValueError("Metadata field is not a string")
            # Never send arbitrary provider metadata, users, tokens, or cookies to agents.
            return CMSPage(external_id=f"{collection}:{data['id']}", url=data["link"],
                title=data["title"]["raw"], content=data["content"]["raw"],
                meta_description=description, status=data["status"], slug=data.get("slug", ""),
                modified_gmt=data.get("modified_gmt", ""), metadata={
                    "provider": "wordpress", "post_type": "page" if collection == "pages" else "post",
                    "meta_description_key": self.meta_description_key,
                    "meta_description_exposed": bool(self.meta_description_key and self.meta_description_key in meta),
                    "atomic_compare_and_swap": False,
                })
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponse("WordPress response lacks authenticated raw page fields") from exc

    def get_page(self, external_id: str) -> CMSPage:
        collection, identifier = self._identity(external_id)
        response = request(self.client, "GET", self._url(f"{collection}/{identifier}"),
                           params={"context": "edit"}, auth=self.auth)
        page = self._page(json_response(response), collection)
        if page.external_id != external_id:
            raise MalformedResponse("WordPress returned a different page identity")
        return page

    def list_pages(self) -> list[CMSPage]:
        pages, seen = [], set()
        for collection in ("pages", "posts"):
            page_number, total_pages = 1, None
            while True:
                response = request(self.client, "GET", self._url(collection), auth=self.auth, params={
                    "context": "edit", "per_page": 100, "page": page_number,
                    "orderby": "id", "order": "asc", "status": "publish,draft,pending,private,future",
                })
                data = json_response(response)
                if not isinstance(data, list):
                    raise MalformedResponse("WordPress inventory must be a list")
                try:
                    current_total = int(response.headers["X-WP-TotalPages"])
                except (KeyError, ValueError) as exc:
                    raise MalformedResponse("WordPress pagination headers missing") from exc
                if current_total < 0 or (total_pages is not None and current_total != total_pages):
                    raise MalformedResponse("WordPress inventory changed during pagination")
                total_pages = current_total
                for raw in data:
                    page = self._page(raw, collection)
                    if page.external_id in seen:
                        raise MalformedResponse("WordPress repeated an inventory page")
                    seen.add(page.external_id)
                    pages.append(page)
                    if len(pages) > self.max_pages:
                        raise ProviderError("CMS inventory exceeds budget; no partial inventory returned")
                if page_number >= total_pages:
                    break
                if not data:
                    raise MalformedResponse("WordPress inventory pagination stalled")
                page_number += 1
        return pages

    def update_page(self, external_id: str, changes: dict[str, Any], *, expected_fingerprint: str) -> CMSPage:
        changes = bounded_changes(changes)
        collection, identifier = self._identity(external_id)
        if not self.allow_optimistic_writes:
            raise ProviderUnavailable("WordPress core lacks atomic preconditions; existing-page writes are disabled")
        before = self.get_page(external_id)
        if not expected_fingerprint or before.fingerprint != expected_fingerprint:
            raise ConcurrencyConflict("CMS page changed after proposal; prepare a new revision")
        payload = {k: v for k, v in changes.items() if k != "meta_description"}
        if "meta_description" in changes:
            if not self.meta_description_key or not before.metadata["meta_description_exposed"]:
                raise ProviderUnavailable("Meta description requires an explicitly registered, exposed WordPress meta key")
            payload["meta"] = {self.meta_description_key: changes["meta_description"]}
        response = request(self.client, "POST", self._url(f"{collection}/{identifier}"),
                           auth=self.auth, json=payload, retry_safe=False)
        try:
            updated = self._page(json_response(response), collection)
            if updated.external_id != external_id or any(getattr(updated, k) != v for k, v in changes.items()):
                raise ValueError("CMS did not preserve requested update")
            if updated.status != before.status or updated.slug != before.slug or updated.url != before.url:
                raise ValueError("CMS changed protected fields")
        except (ProviderError, ValueError) as exc:
            raise AmbiguousWriteError("CMS write response differed; reconcile state") from exc
        return updated

    def create_draft(self, title: str, content: str) -> CMSPage:
        bounded_changes({"title": title, "content": content})
        response = request(self.client, "POST", self._url("pages"), auth=self.auth,
                           json={"title": title, "content": content, "status": "draft"}, retry_safe=False)
        try:
            page = self._page(json_response(response), "pages")
            if page.status != "draft":
                raise ValueError("Provider returned non-draft status")
            return page
        except (ProviderError, ValueError) as exc:
            raise AmbiguousWriteError("CMS draft outcome ambiguous; reconcile before retry") from exc
