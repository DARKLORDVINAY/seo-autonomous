#!/usr/bin/env python3
"""Build the public static demonstration site without reading benchmark labels.

Only pages.json and assets/ are source inputs. The evaluator-only ground truth
is deliberately outside this data flow. No service credentials are accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, SubElement, indent, tostring


SOURCE = Path(__file__).resolve().parent
PATH_PATTERN = re.compile(r"^/(?:[a-z0-9]+(?:-[a-z0-9]+)*/)*$")
PURPOSES = frozenset({"home", "hub", "guide", "note", "exercise", "reference", "utility"})
NAVIGATION = (
    ("/", "Home"), ("/guides/", "Guides"), ("/exercises/", "Exercises"),
    ("/glossary/", "Glossary"), ("/about/", "About"), ("/privacy/", "Privacy"),
)
DISCLAIMER = "Demonstration / test project. Educational examples only; no commercial services or customer claims."


def validate_base_url(value: str, *, fixture: bool = False) -> str:
    """Require an unambiguous public HTTPS origin, with no path or credentials."""
    if fixture:
        if value in {"https://example.test", "https://example.test/"}:
            return "https://example.test"
        raise ValueError("fixture mode accepts only the reserved origin https://example.test")
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("base URL must be a public HTTPS origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base URL is malformed") from exc
    if (
        parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment or port not in (None, 443)
        or "%" in parsed.netloc or "\\" in value or host.endswith(".") or len(host) > 253
    ):
        raise ValueError("base URL must be a public HTTPS origin without credentials, query, fragment, or path")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("use the deployment's public DNS hostname, not an IP address")
    labels = host.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("base URL must contain a valid public DNS hostname")
    if labels[-1] in {"localhost", "local", "internal", "test", "invalid", "onion"}:
        raise ValueError("base URL must use a public deployment hostname")
    return f"https://{host}"


def validate_public_tags(measurement_id: str | None, verification_token: str | None) -> tuple[str, str]:
    measurement_id = measurement_id or ""
    verification_token = verification_token or ""
    if measurement_id and not re.fullmatch(r"G-[A-Z0-9]{10}", measurement_id):
        raise ValueError("GA4_MEASUREMENT_ID must have the form G- followed by 10 uppercase letters or digits")
    if verification_token and not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", verification_token):
        raise ValueError("GSC_VERIFICATION_TOKEN must contain 16–200 URL-safe letters, digits, '_' or '-'")
    return measurement_id, verification_token


def escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def validate_path(path: object) -> str:
    if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
        raise ValueError(f"invalid directory page path: {path!r}")
    return path


def source_pages() -> tuple[dict, list[dict]]:
    source = json.loads((SOURCE / "pages.json").read_text(encoding="utf-8"))
    if source.get("schema_version") != 1 or not isinstance(source.get("pages"), list):
        raise ValueError("unsupported page source schema")
    pages = source["pages"]
    seen = set()
    for page in pages:
        path = validate_path(page["path"])
        if path in seen:
            raise ValueError(f"duplicate page path: {path}")
        seen.add(path)
        if page.get("purpose") not in PURPOSES:
            raise ValueError(f"unsupported page purpose for {path}")
        validate_path(page.get("canonical_path", path))
        if page.get("robots", "index, follow") not in {"index, follow", "noindex, follow"}:
            raise ValueError(f"unsupported robots directive for {path}")
        for link in page.get("links", []):
            validate_path(link["path"])
    for path in source.get("additional_sitemap_paths", []):
        validate_path(path)
    return source, pages


def render_sections(page: dict) -> str:
    sections = []
    for section in page.get("sections", []):
        heading = f"<h2>{escaped(section['heading'])}</h2>" if section.get("heading") else ""
        paragraphs = "".join(f"<p>{escaped(paragraph)}</p>" for paragraph in section.get("paragraphs", []))
        items = "".join(f"<li>{escaped(item)}</li>" for item in section.get("items", []))
        listing = f"<ul>{items}</ul>" if items else ""
        sections.append(f"<section>{heading}{paragraphs}{listing}</section>")
    return "\n".join(sections)


def exercise_markup() -> str:
    return """<section class="exercise" aria-labelledby="exercise-heading">
<h2 id="exercise-heading">Try the three-step page review</h2>
<p>This is a practice interaction, not a purchase, lead, or business conversion. Complete the checks yourself, then record your practice result.</p>
<fieldset><legend>Review any guide in this lab</legend>
<label><input type="checkbox" name="lab-step" value="title"> I compared its browser title with the visible heading.</label>
<label><input type="checkbox" name="lab-step" value="link"> I followed one internal link and checked the destination.</label>
<label><input type="checkbox" name="lab-step" value="purpose"> I considered whether the page answered its stated question.</label>
</fieldset>
<button type="button" id="complete-checklist" disabled>Record practice completion</button>
<p id="exercise-result" class="status" role="status" aria-live="polite">Complete all three checks to enable the button.</p>
<noscript><p>The checklist remains readable without JavaScript. Recording a practice completion requires JavaScript.</p></noscript>
</section>"""


def render_page(page: dict, *, base_url: str, measurement_id: str, verification_token: str) -> str:
    path = page["path"]
    title = page["title"]
    navigation = "".join(
        f'<a href="{target}"{chr(32) + "aria-current=\"page\"" if target == path else ""}>{label}</a>'
        for target, label in NAVIGATION
    )
    tags = []
    if measurement_id:
        tags.append(f'<meta name="lab-ga4-measurement-id" content="{escaped(measurement_id)}">')
    if verification_token:
        tags.append(f'<meta name="google-site-verification" content="{escaped(verification_token)}">')
    related = "".join(
        f'<li><a href="{escaped(link["path"])}">{escaped(link["label"])}</a></li>'
        for link in page.get("links", [])
    )
    related_html = f'<aside class="related" aria-label="Explore further"><h2>Explore further</h2><ul>{related}</ul></aside>' if related else ""
    special = exercise_markup() if page.get("interactive") == "checklist" else ""
    analytics_status = (
        "Optional test analytics is available. It remains off until you choose Allow test analytics."
        if measurement_id else "Analytics is not configured for this deployment. No test analytics events are sent."
    )
    analytics_buttons = (
        '<button type="button" id="analytics-allow">Allow test analytics</button>'
        '<button type="button" id="analytics-decline" class="secondary">Keep analytics off</button>'
        if measurement_id else ""
    )
    canonical = base_url + page.get("canonical_path", path)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped(title)}</title>
<meta name="description" content="{escaped(page['description'])}">
<meta name="robots" content="{escaped(page.get('robots', 'index, follow'))}">
<meta name="lab-page-purpose" content="{escaped(page['purpose'])}">
<link rel="canonical" href="{escaped(canonical)}">
{chr(10).join(tags)}
<link rel="stylesheet" href="/assets/site.css">
<script src="/assets/site.js" defer></script>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to content</a>
<div class="disclosure"><p>{DISCLAIMER}</p></div>
<header class="site-header"><a class="wordmark" href="/" aria-label="Spiral Max SEO Test Lab home"><span class="mark" aria-hidden="true">S</span><span>Spiral Max<span class="wordmark-detail">SEO Test Lab</span></span></a>
<nav aria-label="Primary navigation">{navigation}</nav></header>
<main id="main-content"><article>
<p class="eyebrow">{escaped(page.get('eyebrow', page['purpose'].capitalize()))}</p>
<h1>{escaped(page['heading'])}</h1>
{render_sections(page)}
{special}
</article>
{related_html}
</main>
<footer><p>A small educational demonstration for studying how web pages are discovered, described, and reviewed.</p>
<p>Some pages contain deliberately imperfect examples. Observations here are not evidence of commercial SEO results.</p>
<nav aria-label="Footer navigation"><a href="/about/">About the project</a><a href="/privacy/">Privacy and test analytics</a><a href="/exercises/">Practice exercise</a></nav>
<div class="analytics" aria-labelledby="analytics-heading"><h2 id="analytics-heading">Test analytics</h2>
<p id="analytics-status" role="status">{analytics_status}</p><div class="button-row">{analytics_buttons}</div></div>
</footer>
</body>
</html>
"""


def security_headers(measurement_id: str) -> str:
    script_sources = "'self'"
    connections = "'self'"
    images = "'self' data:"
    if measurement_id:
        script_sources += " https://www.googletagmanager.com"
        connections += " https://*.google-analytics.com https://*.analytics.google.com https://*.googletagmanager.com"
        images += " https://*.google-analytics.com"
    return (
        "/*\n"
        f"  Content-Security-Policy: default-src 'self'; script-src {script_sources}; style-src 'self'; "
        f"img-src {images}; connect-src {connections}; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'; upgrade-insecure-requests\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: no-referrer\n"
        "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        "/inventory.json\n"
        "  X-Robots-Tag: noindex\n"
        "  Cache-Control: public, max-age=0, must-revalidate\n"
        "/404.html\n"
        "  X-Robots-Tag: noindex\n"
    )


def build_site(base_url: str, output: Path | str, *, measurement_id: str | None = None,
               verification_token: str | None = None, fixture: bool = False) -> dict:
    base_url = validate_base_url(base_url, fixture=fixture)
    measurement_id, verification_token = validate_public_tags(measurement_id, verification_token)
    if fixture and (measurement_id or verification_token):
        raise ValueError("fixture releases cannot contain analytics or verification identifiers")
    output = Path(output).resolve()
    if output == SOURCE or output in SOURCE.parents:
        raise ValueError("output must not overwrite the source directory or its ancestors")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be empty; choose a new release directory")
    source, pages = source_pages()
    output.mkdir(parents=True, exist_ok=True)
    inventory = {"schema_version": 1, "base_url": base_url, "pages": []}
    for page in pages:
        directory = output / page["path"].strip("/")
        directory.mkdir(parents=True, exist_ok=True)
        body = render_page(page, base_url=base_url, measurement_id=measurement_id,
                           verification_token=verification_token).encode("utf-8")
        (directory / "index.html").write_bytes(body)
        inventory["pages"].append({
            "path": page["path"], "content_sha256": hashlib.sha256(body).hexdigest(),
            "desired_indexing": "index", "purpose": page["purpose"],
        })
    missing_page = {
        "path": "/404/", "purpose": "utility", "title": "Page not found | Spiral Max SEO Test Lab",
        "description": "This demonstration page could not be found. Return to the guides to continue exploring.",
        "heading": "That page is not here", "robots": "noindex, follow",
        "sections": [{"paragraphs": [
            "The address may be outdated or the page may not exist. This is a demonstration site, and some links are intentionally imperfect examples.",
            "Use the navigation to return to a guide or the practice checklist."
        ]}],
    }
    (output / "404.html").write_text(render_page(
        missing_page, base_url=base_url, measurement_id=measurement_id, verification_token=verification_token,
    ), encoding="utf-8")
    shutil.copytree(SOURCE / "assets", output / "assets")
    sitemap = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    sitemap_paths = [page["path"] for page in pages if page.get("in_sitemap", True)]
    sitemap_paths.extend(source.get("additional_sitemap_paths", []))
    for path in sorted(sitemap_paths):
        SubElement(SubElement(sitemap, "url"), "loc").text = base_url + path
    indent(sitemap)
    (output / "sitemap.xml").write_bytes(tostring(sitemap, encoding="utf-8", xml_declaration=True))
    (output / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base_url}/sitemap.xml\n", encoding="utf-8")
    (output / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    (output / "_headers").write_text(security_headers(measurement_id), encoding="utf-8")
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Public HTTPS origin, e.g. https://my-project.pages.dev")
    parser.add_argument("--output", required=True, type=Path, help="New or empty static release directory")
    parser.add_argument("--fixture", action="store_true", help="Allow only https://example.test for isolated fixture checks")
    args = parser.parse_args()
    try:
        inventory = build_site(
            args.base_url, args.output,
            measurement_id=os.environ.get("GA4_MEASUREMENT_ID"),
            verification_token=os.environ.get("GSC_VERIFICATION_TOKEN"),
            fixture=args.fixture,
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"Build failed: {exc}\n")
    print(json.dumps({"pages": len(inventory["pages"]), "base_url": inventory["base_url"],
                      "output": str(args.output), "fixture": args.fixture}))


if __name__ == "__main__":
    main()
