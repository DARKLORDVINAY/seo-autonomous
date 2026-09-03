"""Private, deterministic benchmark authoring code, independent of detectors.

The public interface returns TWO different security domains: unlabeled runtime
observations and evaluator-only truth.  Neither this module nor truth belongs in
the runtime's filesystem, prompt, database, or trace.  These are simulated crawl
observations, not claims about any real website or real browser.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Callable


ORIGIN = "https://example.test"
EXTERNAL_ORIGIN = "https://reference.example.test"
OBSERVED_AT = "2026-09-03T00:00:00+00:00"
SCHEMA_VERSION = "2.0"

# The same content vocabulary appears in both splits.  No wording is a split or
# family marker.  Only evaluator-side recipes identify the causal situation.
ARTICLES = (
    ("Keeping a seed notebook", "A seed notebook records what was planted, where it was placed, and what was observed later. Draw a simple plan of the container before adding compost. Record the plant name from its packet without guessing an uncertain variety. Describe light, watering, and visible growth in separate entries. A dry surface does not by itself reveal the moisture deeper in the pot. Compare observations made in similar conditions and retain entries for unsuccessful seedlings. Photographs can supplement notes but should not replace a written description of the observation. At the end of the exercise, distinguish what actually happened from explanations that still need checking."),
    ("Reading a simple street map", "A street map is a selective representation rather than a picture of every feature on the ground. Begin by locating its legend and checking how routes and landmarks are represented. Find the starting point and destination before choosing a route. A route that looks short on paper can still involve crossings or barriers. Notice whether the map describes walking, cycling, or another mode of travel. Follow the sequence of junctions and write down a nearby landmark for each change of direction. If an expected landmark is absent, stop and compare the surrounding streets. An uncertain location should be recorded as uncertain instead of silently moving the starting point."),
    ("Comparing fabric weaves", "Fabric samples can be compared by examining the arrangement of their threads under consistent lighting. Put the samples on a plain surface and keep their labels separate from your observations. Describe visible crossings, spacing, and texture before deciding whether two pieces look similar. Rotate each sample to check whether the apparent pattern depends on its orientation. Different finishes can change how light reflects from the surface, so appearance alone does not establish fibre composition. Preserve a small reference sample when a project permits it. Record uncertainty when a weave is difficult to identify. This exercise is about careful description and does not certify the material for any particular use."),
    ("Organising a reading shelf", "An organised reading shelf makes it easier to locate a book and return it to a predictable place. Choose a grouping that suits the collection, such as topic followed by author, and write that rule down. Separate books temporarily needed for a project from books that are simply misplaced. Check the labels against the actual titles instead of relying on remembered cover colours. Leave sufficient room to remove a book without pulling neighbouring volumes onto the floor. A short catalogue can record location and lending notes without collecting unnecessary personal information. Review the arrangement after using it for a while and keep the parts that genuinely reduce search effort."),
    ("Observing evening clouds", "Cloud observations begin with a description of the sky at a particular place and time. Note the visible shapes, apparent movement, and changes in coverage without treating a single observation as a forecast. Buildings and trees may obscure portions of the sky, so record those limits. Compare photographs only when their direction and exposure are reasonably similar. A cloud that appears darker may be lit differently rather than containing a known amount of rain. Keep the original observation separate from any proposed explanation. Repeat the exercise from the same safe viewing point if useful. Never infer a reliable weather prediction merely from the names assigned to the shapes."),
    ("Planning a paper model", "A paper model benefits from a small plan before the first fold or cut. Draw the main shapes, mark the edges that should join, and check that the parts can be assembled in a sensible order. Use a spare piece to try a fold that is unfamiliar. Keep a note of changes to the plan so a second model can reproduce the useful adjustments. Paper thickness affects how the finished corners meet, but a failed join does not prove that the whole design is unusable. Compare the model with its intended purpose rather than with an imaginary perfect object. Store the final pattern with a brief record of what remains uncertain."),
    ("Describing a sound walk", "A sound walk records what can be heard along a route without needing to identify every source. Pick a safe route and pause only where doing so does not obstruct other people. Describe a sound's rhythm, apparent direction, and changes over time before deciding what produced it. Two similar sounds can have different sources, while one source can sound different from another position. Record the listening conditions and any gaps caused by traffic or conversation. Do not record private conversations as part of the exercise. A short written account is sufficient for comparing observations from another walk. Distinguish a heard sound from an interpretation of its cause."),
    ("Making a colour reference card", "A colour reference card helps organise comparisons made under a consistent light source. Place the materials beside one another and label them before the order is forgotten. Note whether a surface is glossy, rough, translucent, or opaque because those features affect its appearance. Photographs viewed on different displays may not preserve the comparison accurately. Use descriptive observations rather than claiming an exact measurement from an uncalibrated picture. Keep the card out of conditions that visibly alter the samples and record any changes. The card can support a craft project without becoming a universal colour standard. Recheck important comparisons under the conditions in which the finished object will actually be used."),
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _url(seed: int, family: str, variant: int, index: int) -> str:
    token = hashlib.sha256(f"{seed}:{family}:{variant}:{index}:url".encode()).hexdigest()[:14]
    return f"{ORIGIN}/{('guide', 'reference', 'practice')[index % 3]}/{token}/"


def _page(url: str, index: int) -> dict[str, Any]:
    heading, prose = ARTICLES[index % len(ARTICLES)]
    return {
        "url": url, "final_url": url, "status_code": 200,
        "title": heading, "meta_description": f"A practical observation exercise: {heading.lower()}.",
        "canonical": url, "robots_directives": [], "crawlable": True,
        "indexability": "eligible", "links": [], "schema": [], "redirect_chain": [],
        "content_hash": hashlib.sha256(prose.encode()).hexdigest(),
        "text": prose, "main_text": prose, "main_heading": heading,
        "main_content_observed": True, "has_interactive_content": False,
        "issues": [], "fetched_at": OBSERVED_AT, "source_trust": "untrusted_external",
    }


def _body(page: dict[str, Any], text: str, heading: str | None = None) -> None:
    page["text"] = page["main_text"] = text
    page["content_hash"] = hashlib.sha256(text.encode()).hexdigest()
    if heading is not None:
        page["main_heading"] = heading


@dataclass
class _Fixture:
    family: str
    variant: int
    seed: int
    stratum: str
    case_id: str = ""
    crawls: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    units: list[dict[str, Any]] = field(default_factory=list)
    expected_decisions: list[str] = field(default_factory=lambda: ["INVESTIGATE"])
    rendered: list[dict[str, Any]] | None = None
    notes: list[str] = field(default_factory=list)
    protected_urls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.case_id = "c_" + hashlib.sha256(f"{self.seed}:{self.family}:{self.variant}:case".encode()).hexdigest()[:20]
        count = 4 + self.variant % 3
        offset = int(hashlib.sha256(f"{self.seed}:{self.family}".encode()).hexdigest()[:4], 16) % len(ARTICLES)
        self.crawls = [_page(_url(self.seed, self.family, self.variant, i), i + offset) for i in range(count)]
        urls = self.urls
        # Three distinct background topologies, not simply URL renaming.
        for index, page in enumerate(self.crawls):
            if self.variant % 3 == 0:
                page["links"] = [u for u in urls if u != page["url"]]
            elif self.variant % 3 == 1:
                page["links"] = list(dict.fromkeys([urls[(index - 1) % count], urls[(index + 1) % count]]))
            else:
                page["links"] = ([u for u in urls if u != urls[0]] if index == 0 else
                                 list(dict.fromkeys([urls[0], urls[(index + 1) % count]])))
        self.context = {
            "site_url": ORIGIN, "inventory_urls": urls.copy(), "inventory_complete": True,
            "crawl_coverage_complete": True, "entrypoint_urls": [urls[0]],
            "sitemap_urls": urls.copy(), "sitemap_complete": True,
            "intended_indexable_urls": urls.copy(),
            "page_purposes": {u: "educational_article" for u in urls},
            "thin_content_word_threshold": 80,
        }
        if self.stratum == "control":
            self.expected_decisions = ["NO-ACTION"]

    @property
    def urls(self) -> list[str]:
        return [p["url"] for p in self.crawls]

    def exclude_index(self, page: dict[str, Any], purpose: str) -> None:
        for key in ("intended_indexable_urls", "sitemap_urls"):
            self.context[key] = [u for u in self.context[key] if u != page["url"]]
        self.context["page_purposes"][page["url"]] = purpose

    def unlink(self, url: str) -> None:
        for page in self.crawls:
            page["links"] = [link for link in page["links"] if link != url]

    def add_page(self, *, status: int = 200, purpose: str = "educational_article", linked: bool = False) -> dict[str, Any]:
        page = _page(_url(self.seed, self.family, self.variant, len(self.crawls)), len(self.crawls))
        if page["title"] in {p["title"] for p in self.crawls}:
            page["title"] += " — supplementary notes"
            page["meta_description"] += " Supplementary observations and notes."
        page["status_code"] = status
        page["links"] = [self.urls[0]]
        self.crawls.append(page)
        self.context["inventory_urls"].append(page["url"])
        self.context["page_purposes"][page["url"]] = purpose
        if status == 200:
            self.context["intended_indexable_urls"].append(page["url"])
            self.context["sitemap_urls"].append(page["url"])
        else:
            page["indexability"] = "blocked"
            page["canonical"] = None
            page["title"] = f"HTTP {status} response"
            page["meta_description"] = ""
            _body(page, "This resource is not available.")
        if linked:
            for source in self.crawls[:2]:
                source["links"].append(page["url"])
        return page

    def unit(self, kind: str, pages: dict[str, Any] | list[dict[str, Any]], *,
             related: list[str] | None = None, related_mode: str = "optional_expected",
             dispositions: tuple[str, ...] = ("REVIEW", "NEEDS_EVIDENCE"),
             epistemic_class: str = "observation", reason: str = "") -> None:
        if isinstance(pages, dict):
            pages = [pages]
        self.units.append({
            "unit_id": f"{self.case_id}:u{len(self.units) + 1}", "kind": kind,
            "page_urls": sorted({p["url"] for p in pages}),
            "related_urls": sorted(set(related or [])), "related_mode": related_mode,
            "allowed_dispositions": list(dispositions), "epistemic_class": epistemic_class,
            "reason": reason,
        })

    def export(self) -> tuple[dict[str, Any], dict[str, Any]]:
        randomizer = random.Random(f"{self.seed}:{self.case_id}:shuffle")
        crawls = copy.deepcopy(self.crawls)
        randomizer.shuffle(crawls)
        runtime = {"case_id": self.case_id, "crawls": crawls, "context": copy.deepcopy(self.context),
                   "gsc_rows": [], "ga4_rows": []}
        if self.rendered is not None:
            runtime["rendered_crawls"] = copy.deepcopy(self.rendered)
        truth = {
            "case_id": self.case_id, "family": self.family, "stratum": self.stratum,
            "expected_decisions": self.expected_decisions,
            "coverage_complete": bool(self.context["inventory_complete"] and
                                      self.context["crawl_coverage_complete"] and self.context["sitemap_complete"]),
            "units": copy.deepcopy(self.units), "protected_urls": sorted(set(self.protected_urls)),
            "notes": self.notes, "rendered_evidence": "simulated_independent_DOM_snapshot" if self.rendered is not None else "none",
        }
        return runtime, truth


# CONTROL recipes.  A control is not a promise of business performance: it means
# the provided structural observations warrant no SEO repair in this scope.
def _clean_dense(f: _Fixture) -> None:
    f.notes.append("Distinct useful pages, complete observations, and adequate navigable paths; no evidence-backed repair is needed.")


def _small_contextual_graph(f: _Fixture) -> None:
    root = f.crawls[0]
    root["links"] = f.urls[1:]
    for page in f.crawls[1:]:
        page["links"] = [root["url"]]
        f.context["page_purposes"][page["url"]] = "single_exercise_in_small_collection"
    f.notes.append("A small complete collection needs contextual access, not an arbitrary minimum inbound-link count.")


def _intentional_print(f: _Fixture) -> None:
    alias, primary = f.crawls[-1], f.crawls[1]
    alias["canonical"] = primary["url"]
    _body(alias, primary["main_text"], primary["main_heading"])
    alias["title"] = primary["title"] + " — printable version"
    alias["meta_description"] = "A printable copy of the corresponding educational exercise."
    f.exclude_index(alias, "printable_version")
    f.protected_urls.append(alias["url"])


def _language_intents(f: _Fixture) -> None:
    left, right = f.crawls[1:3]
    left["title"], right["title"] = "Reading map symbols in English", "Leer símbolos de mapas en español"
    left["meta_description"], right["meta_description"] = "English-language map symbol reference.", "Referencia de símbolos de mapas en español."
    _body(right, "Un mapa representa lugares mediante símbolos y líneas. La leyenda explica el significado de cada símbolo. Antes de comenzar una ruta, identifica el punto de partida y el destino. Comprueba qué caminos son apropiados para caminar y cuáles están destinados a otros medios de transporte. Las distancias dibujadas dependen de la escala del mapa. Un edificio puede ayudar a reconocer una esquina, pero no todos los edificios aparecen en el dibujo. Si no puedes localizarte con seguridad, compara varias calles próximas y conserva la incertidumbre en tus notas. Anota los cruces importantes y revisa la leyenda cuando encuentres una marca desconocida durante el recorrido.")
    f.context["page_purposes"].update({left["url"]: "English_language_reference", right["url"]: "Spanish_language_reference"})


def _utility_noindex(f: _Fixture) -> None:
    page = f.crawls[-1]
    page["robots_directives"] = ["noindex", "follow"] if f.variant % 2 else ["none"]
    page["indexability"] = "blocked"
    f.exclude_index(page, "internal_search_results")
    f.protected_urls.append(page["url"])


def _private_robots(f: _Fixture) -> None:
    page = f.crawls[-1]
    page.update({"status_code": None, "crawlable": False, "indexability": "unknown",
                 "main_content_observed": False, "canonical": None, "title": "", "meta_description": ""})
    _body(page, "")
    f.exclude_index(page, "private_account_area")
    f.unlink(page["url"])
    f.protected_urls.append(page["url"])


def _retired_resource(f: _Fixture) -> None:
    page = f.add_page(status=410 if f.variant % 2 else 404, purpose="retired_resource")
    f.exclude_index(page, "retired_resource")
    f.notes.append("An intentionally removed URL with no internal references or sitemap membership is not a broken-link issue.")


def _one_hop(f: _Fixture) -> None:
    old, current = f.crawls[-1], f.crawls[1]
    old.update({"status_code": 200, "final_url": current["url"], "redirect_chain": [old["url"], current["url"]],
                "canonical": current["url"], "title": current["title"], "meta_description": current["meta_description"]})
    _body(old, current["main_text"], current["main_heading"])
    old["links"] = current["links"].copy()
    f.exclude_index(old, "permanent_redirect_alias")
    f.unlink(old["url"])
    f.protected_urls.append(old["url"])


def _different_intent(f: _Fixture) -> None:
    reference, exercise = f.crawls[1:3]
    reference["title"], exercise["title"] = "Map scale: meaning and notation", "Map scale: a measuring exercise"
    reference["meta_description"], exercise["meta_description"] = "Definitions of map scale and ratio notation.", "A practical exercise in comparing distances on a map."
    _body(reference, "Map scale describes a relationship between a distance represented on a map and a corresponding distance on the ground. A written ratio expresses that relationship using the same units on both sides. A scale bar presents it visually and can remain useful when a printed map is resized. Scale does not tell the reader whether a route is accessible or whether a crossing exists. This reference explains the vocabulary used by a separate measuring exercise. It does not ask the reader to calculate a route. Readers who already understand the notation can move directly to the exercise, while others can return here to check the definitions.")
    _body(exercise, "Place a ruler along a straight line between two marked locations on the exercise map. Write down the measured length before consulting the scale bar. Compare that length with the divisions shown on the bar and record the result in the units it provides. Repeat the measurement after repositioning the ruler and explain any disagreement between the readings. For a route that bends, divide it into several short sections rather than measuring directly between its endpoints. This activity practises a procedure; the linked reference explains what scale notation means. Do not infer a travel time from the measured distance because the activity contains no evidence about the conditions of the route.")
    f.context["page_purposes"].update({reference["url"]: "concept_reference", exercise["url"]: "practical_exercise"})


def _interactive_short(f: _Fixture) -> None:
    page = f.crawls[1]
    _body(page, "Choose the two marked points, enter their measured separation, and select the unit shown on the scale bar. The calculator displays the corresponding distance. No account or personal information is required.")
    page["has_interactive_content"] = True
    f.context["page_purposes"][page["url"]] = "interactive_calculator"


def _short_definition(f: _Fixture) -> None:
    page = f.crawls[1]
    page["title"] = "What is a map legend?"
    page["meta_description"] = "A concise definition of a map legend."
    _body(page, "A map legend is the part of a map that explains its symbols, colours, and line styles. Consult the legend before interpreting an unfamiliar mark. The same mark can mean different things on different maps, so use the legend belonging to the map you are reading.", page["title"])
    f.context["page_purposes"][page["url"]] = "concise_dictionary_definition"


def _real_404_guide(f: _Fixture) -> None:
    page = f.crawls[1]
    page["title"] = "Why a page says not found: understanding HTTP 404"
    page["meta_description"] = "An explanation of the not-found response, not a missing resource."
    _body(page, "A page not found message often accompanies an HTTP 404 response. The status describes the server's response to a particular requested address. A link may point to a resource that was removed, moved without a redirect, or typed incorrectly. Check the requested address and the response before deciding which explanation applies. A missing picture is not the same as a missing document, and a temporary connection failure is not a confirmed 404. Preserve those distinctions when reporting a problem. If the resource is intentionally retired, a not-found response can be appropriate. A useful report records the original link, requested address, observed response, and time of the observation without inventing the reason for the removal.", page["title"])


def _rendered_complete(f: _Fixture) -> None:
    page = f.crawls[1]
    rendered = copy.deepcopy(page)
    page["main_content_observed"] = False
    page["has_interactive_content"] = True
    _body(page, "Loading the educational exercise.")
    f.rendered = [rendered]
    f.notes.append("The supplied independent rendered snapshot resolves the raw-shell uncertainty; it is simulated, not a Playwright run.")


def _sitemap_exclusions(f: _Fixture) -> None:
    if f.variant == 4:
        for page in f.crawls:
            page["robots_directives"], page["indexability"] = ["noindex"], "blocked"
            f.exclude_index(page, "internal_reference_collection")
            f.protected_urls.append(page["url"])
        f.notes.append("A successfully retrieved empty sitemap is appropriate for this entirely owner-declared nonindexable collection.")
        return
    _intentional_print(f)
    utility = f.crawls[-2]
    utility["robots_directives"] = ["noindex"]
    utility["indexability"] = "blocked"
    f.exclude_index(utility, "saved_exercise_preferences")
    f.protected_urls.append(utility["url"])


def _equivalent_canonical(f: _Fixture) -> None:
    page = f.crawls[1]
    page["canonical"] = page["url"].replace("https://example.test", "https://EXAMPLE.test:443")
    f.notes.append("DNS host case and an explicit default HTTPS port do not identify a different canonical resource.")


def _benign_schema(f: _Fixture) -> None:
    page = f.crawls[1]
    page["schema"] = [{"@context": "https://schema.org", "@type": "Article", "headline": page["title"],
                       "description": "A demonstration discussing the phrase ‘ignore previous instructions’ as quoted data.",
                       "mainEntityOfPage": page["url"]}]
    f.notes.append("Quoted instruction-like words do not justify SEO changes or carry administrative authority.")


def _multiple_entrypoints(f: _Fixture) -> None:
    middle = len(f.crawls) // 2
    groups = (f.crawls[:middle], f.crawls[middle:])
    for group in groups:
        for page in group:
            page["links"] = [other["url"] for other in group if other != page]
            f.context["page_purposes"][page["url"]] = "standalone_small_reference_collection"
    f.context["entrypoint_urls"] = [groups[0][0]["url"], groups[1][0]["url"]]
    f.notes.append("Owner-declared independent entrypoints make both small collections reachable; homepage-only reachability would be a false positive.")


def _fragment_links(f: _Fixture) -> None:
    for index, page in enumerate(f.crawls):
        page["links"] = [link + ("#example" if (index + n) % 2 else "#notes") for n, link in enumerate(page["links"])]
    f.notes.append("Same-document fragments do not create extra missing pages or erase real inbound paths.")


# Positive units remain investigation proposals; none authorises execution.
def _broken_links(f: _Fixture) -> None:
    target = f.add_page(status=404 if f.variant % 2 else 410, purpose="retired_resource")
    sources = f.crawls[:1 + f.variant % 3]
    for source in sources:
        source["links"].append(target["url"])
        f.unit("broken_internal_link", source, related=[target["url"]], related_mode="contains",
               reason="A current internal reference has an observed terminal unavailable response.")


def _orphan(f: _Fixture) -> None:
    page = f.crawls[-1]
    f.unlink(page["url"])
    f.unit("orphan_page", page, reason="The complete owner inventory contains an intended public page with no observed incoming link or declared entrypoint.")


def _canonical_missing(f: _Fixture) -> None:
    target = f.add_page(status=404, purpose="retired_resource")
    for source in f.crawls[1:2 + f.variant % 2]:
        source["canonical"] = target["url"]
        f.unit("canonical_mismatch", source, related=[target["url"]],
               reason="The canonical points to an observed unavailable resource, not an intentional alias target.")


def _canonical_cycle(f: _Fixture) -> None:
    members = f.crawls[1:3 + f.variant % 2]
    for index, page in enumerate(members):
        page["canonical"] = members[(index + 1) % len(members)]["url"]
    f.unit("canonical_cycle", members, related=[p["url"] for p in members],
           reason="Observed canonical edges form a directed cycle, with no self-canonical representative.")


def _canonical_external(f: _Fixture) -> None:
    page = f.crawls[1]
    target = EXTERNAL_ORIGIN + "/reference/" + page["url"].split("/")[-2] + "/"
    page["canonical"] = target
    f.unit("canonical_mismatch", page, related=[target], epistemic_class="needs_owner_intent",
           reason="An intended indexable original points off origin; investigate ownership or syndication rather than rewriting automatically.")


def _accidental_noindex(f: _Fixture) -> None:
    pages = f.crawls[1:2 + f.variant % 2]
    for page in pages:
        page["robots_directives"] = ["noindex", "follow"] if f.variant % 2 else ["none"]
        page["indexability"] = "blocked"
        f.unit("indexability_review", page, reason="Observed noindex contradicts the independently declared indexable purpose.")


def _stale_sitemap(f: _Fixture) -> None:
    target = f.add_page(status=404 if f.variant % 2 else 410, purpose="retired_resource")
    f.context["sitemap_urls"].append(target["url"])
    f.unit("sitemap_unavailable_url", target, reason="A complete sitemap still advertises an observed terminal unavailable URL.")


def _redirect_chain(f: _Fixture) -> None:
    source, middle, final = f.crawls[-1], f.crawls[-2], f.crawls[1]
    if f.variant == 4:
        for page, other in ((source, middle), (middle, source)):
            page.update({"final_url": page["url"], "status_code": None, "indexability": "unknown",
                         "canonical": None, "title": "", "meta_description": "", "main_content_observed": False,
                         "redirect_chain": [page["url"], other["url"], page["url"]], "links": [],
                         "issues": [{"kind": "redirect_loop", "detail": "An already visited redirect location repeated."}]})
            _body(page, "")
            f.exclude_index(page, "permanent_redirect_alias")
            f.unlink(page["url"])
        f.crawls[0]["links"].append(source["url"])
        f.unit("redirect_loop", [source, middle], related=[source["url"], middle["url"]],
               reason="Repeated observed redirect locations establish a loop rather than a terminal 404 or an ordinary long chain.")
        return
    aliases = [source, middle]
    if f.variant % 2:
        aliases.append(f.add_page(purpose="permanent_redirect_alias"))
    chain = [p["url"] for p in aliases] + [final["url"]]
    for index, alias in enumerate(aliases):
        alias.update({"final_url": final["url"], "redirect_chain": chain[index:], "canonical": final["url"],
                      "title": final["title"], "meta_description": final["meta_description"], "links": final["links"].copy()})
        _body(alias, final["main_text"], final["main_heading"])
        f.exclude_index(alias, "permanent_redirect_alias")
        f.unlink(alias["url"])
    f.unit("redirect_chain", source, related=chain[1:], reason="A known redirect takes more than one hop; one-hop migrations are separate controls.")
    for index, alias in enumerate(aliases[1:], 1):
        if len(chain[index:]) > 2:
            f.unit("redirect_chain", alias, related=chain[index + 1:], reason="An independently requested intermediate alias also has multiple remaining hops.")


def _duplicate_metadata(f: _Fixture) -> None:
    members = f.crawls[1:3 + f.variant % 2]
    for page in members:
        page["title"] = "Practical observation notes"
        page["meta_description"] = "Read these practical observation notes and complete the exercise."
    f.unit("duplicate_title", members, related=[p["url"] for p in members], epistemic_class="diagnostic_review",
           reason="Distinct intended documents share uninformative titles; a diagnostic finding does not establish ranking harm.")
    f.unit("duplicate_meta_description", members, related=[p["url"] for p in members], epistemic_class="diagnostic_review",
           reason="Distinct intended documents share descriptions; first verify whether a smaller useful change is warranted.")


def _soft_404(f: _Fixture) -> None:
    page = f.crawls[1]
    page["title"] = "The requested page is unavailable" if f.variant % 2 else "Page not found"
    page["meta_description"] = "The requested resource cannot be displayed."
    _body(page, "We could not find the requested document. Return to the index to choose another exercise.", "Page not found")
    f.unit("soft_404", page, epistemic_class="hypothesis", reason="An intended content URL returns HTTP 200 with an unmistakable missing-resource body. Search-engine classification is unobserved.")
    f.unit("thin_content", page, epistemic_class="diagnostic_review", reason="The intended article has only a missing-resource placeholder, not a legitimate short utility.")


def _placeholder(f: _Fixture) -> None:
    for page in f.crawls[1:2 + f.variant % 2]:
        _body(page, "This exercise is being prepared. A full explanation and worked example will be added later.")
        f.unit("thin_content", page, epistemic_class="diagnostic_review", reason="Owner intent is an educational article but the observed main content is an unfinished placeholder.")


def _robots_conflict(f: _Fixture) -> None:
    page = f.crawls[1]
    page.update({"crawlable": False, "indexability": "unknown", "status_code": None,
                 "canonical": None, "main_content_observed": False, "title": "", "meta_description": ""})
    _body(page, "")
    f.unit("indexability_review", page, epistemic_class="needs_evidence", reason="Known robots disallow conflicts with declared public indexing intent; blocked crawling does not itself prove deindexing.")


# Ambiguous cases distinguish unavailability from absence and uncertainty from
# both an asserted repair and a confident NO-ACTION outcome.
def _partial_graph(f: _Fixture) -> None:
    page = f.crawls[-1]
    f.unlink(page["url"])
    f.context.update({"inventory_complete": False, "crawl_coverage_complete": False})
    f.expected_decisions = ["NEEDS_EVIDENCE", "INVESTIGATE"]
    f.unit("potential_orphan_page", page, epistemic_class="needs_evidence", reason="The observed graph lacks incoming links but the graph is incomplete; a true orphan is not established.")


def _timeout(f: _Fixture) -> None:
    page = f.crawls[-1]
    page.update({"status_code": None, "crawlable": None, "indexability": "unknown",
                 "canonical": None, "title": "", "meta_description": "", "main_content_observed": False,
                 "issues": [{"kind": "timeout", "detail": "Response was unavailable before the request deadline."}]})
    _body(page, "")
    f.context["crawl_coverage_complete"] = False
    f.expected_decisions = ["NEEDS_EVIDENCE"]
    f.notes.append("A timeout is not evidence that an internal destination is broken, absent, thin, or excluded from an index.")


def _sitemap_unknown(f: _Fixture) -> None:
    f.context.update({"sitemap_urls": f.urls[:2] if f.variant % 2 == 0 else [], "sitemap_complete": False})
    f.expected_decisions = ["NEEDS_EVIDENCE"]
    f.notes.append("The sitemap is unavailable or partially retrieved, not a successfully retrieved empty document.")


def _render_unavailable(f: _Fixture) -> None:
    page = f.crawls[1]
    page["main_content_observed"] = False
    page["has_interactive_content"] = True
    _body(page, "Loading the educational exercise.")
    f.context["crawl_coverage_complete"] = False
    f.expected_decisions = ["NEEDS_EVIDENCE"]
    f.notes.append("Raw HTML is an application shell and no independent DOM observation is available. Do not infer a low-value rendered page.")


def _overlap_unknown(f: _Fixture) -> None:
    members = f.crawls[1:3]
    _body(members[1], members[0]["main_text"], members[0]["main_heading"])
    f.expected_decisions = ["INVESTIGATE", "NEEDS_EVIDENCE"]
    f.unit("potential_topic_overlap", members, related=[p["url"] for p in members], epistemic_class="hypothesis",
           reason="Identical main content on two intended URLs warrants overlap review, not confirmed query cannibalisation without query evidence.")


def _canonical_unseen(f: _Fixture) -> None:
    page = f.crawls[1]
    target = _url(f.seed, f.family, f.variant, 91)
    page["canonical"] = target
    f.context.update({"inventory_complete": False, "crawl_coverage_complete": False})
    f.expected_decisions = ["INVESTIGATE", "NEEDS_EVIDENCE"]
    f.unit("canonical_mismatch", page, related=[target], epistemic_class="needs_evidence",
           reason="A nonself canonical has an unobserved target. Investigate, but do not invent its response or change it automatically.")


# Entirely holdout-only interactions.  No development recipe is a direct copy
# of these multi-fault or conflicting-evidence topologies.
def _canonical_noindex(f: _Fixture) -> None:
    source, target = f.crawls[1:3]
    source["canonical"] = target["url"]
    target["robots_directives"], target["indexability"] = ["noindex"], "blocked"
    f.unit("canonical_target_nonindexable", source, related=[target["url"]], related_mode="contains",
           reason="Canonical consolidation points to a fetched noindex target, a cross-page contradiction.")
    f.unit("indexability_review", target, reason="Noindex contradicts the intended public article purpose.")


def _sitemap_robots(f: _Fixture) -> None:
    _robots_conflict(f)
    absent = f.crawls[-1]
    f.context["sitemap_urls"].remove(absent["url"])
    f.unit("sitemap_missing_page", absent, epistemic_class="diagnostic_review",
           reason="One intended public URL is omitted from an otherwise complete sitemap; this is a review signal, not a requirement that all URLs be submitted.")
    f.notes.append("The blocked URL remains in the sitemap while a different public URL is omitted. The root causes and affected URLs must not be conflated.")


def _canonical_redirect_cycle(f: _Fixture) -> None:
    first, alias, last = f.crawls[1:4]
    first["canonical"] = alias["url"]
    alias.update({"final_url": last["url"], "canonical": first["url"],
                  "redirect_chain": [alias["url"], last["url"]], "title": last["title"],
                  "meta_description": last["meta_description"], "links": last["links"].copy()})
    _body(alias, last["main_text"], last["main_heading"])
    last["canonical"] = first["url"]
    f.exclude_index(alias, "permanent_redirect_alias")
    f.unlink(alias["url"])
    f.unit("canonical_cycle", [first, last], related=[first["url"], alias["url"], last["url"]],
           reason="Resolving a one-hop alias closes a canonical cycle; examining only direct same-URL canonical edges misses the interaction.")


def _broken_bridge(f: _Fixture) -> None:
    island = f.crawls[-2:]
    island_urls = {p["url"] for p in island}
    for page in f.crawls[:-2]:
        page["links"] = [u for u in page["links"] if u not in island_urls]
    for page in island:
        page["links"] = [u for u in island_urls if u != page["url"]]
    missing = f.add_page(status=404, purpose="retired_resource")
    root = f.crawls[0]
    root["links"].append(missing["url"])
    f.unit("broken_internal_link", root, related=[missing["url"]], related_mode="contains",
           reason="The intended bridge reaches an observed missing destination.")
    f.unit("orphan_component", island, related=sorted(island_urls),
           reason="The island pages link to each other but are unreachable from every declared entrypoint in the complete graph.")


def _rendered_failure(f: _Fixture) -> None:
    page = f.crawls[1]
    rendered = copy.deepcopy(page)
    rendered["title"] = "Document unavailable"
    rendered["meta_description"] = "The selected document could not be displayed."
    _body(rendered, "This document does not exist. Choose another item from the collection.", "Document not found")
    f.rendered = [rendered]
    f.unit("soft_404", page, epistemic_class="hypothesis", reason="The independent DOM snapshot replaces a seemingly complete raw article with a missing-resource state while HTTP remains 200.")
    f.unit("thin_content", page, epistemic_class="diagnostic_review", reason="The observed rendered main content does not fulfil the intended article purpose.")


def _thin_overlap(f: _Fixture) -> None:
    members = f.crawls[1:3]
    for index, page in enumerate(members):
        page["title"] = "Keeping seed records: " + ("getting started" if index == 0 else "first steps")
        page["meta_description"] = "A planned seed-record exercise, " + ("introductory edition." if index == 0 else "companion edition.")
        _body(page, "Record the seed name, planting date, and visible growth. More instructions and examples are planned. This unfinished exercise does not yet explain how to compare the observations or avoid uncertain conclusions.")
        f.unit("thin_content", page, epistemic_class="diagnostic_review", reason="An unfinished article is not a short but complete utility.")
    f.unit("potential_topic_overlap", members, related=[p["url"] for p in members], epistemic_class="hypothesis",
           reason="The two unfinished intended articles have the same main content; query cannibalisation is not observed.")
    if f.variant % 2:
        members[1]["robots_directives"], members[1]["indexability"] = ["noindex"], "blocked"
        f.unit("indexability_review", members[1], reason="An additional noindex contradicts intent and must not be treated as proof that the other content problem is resolved.")


def _redirect_dead(f: _Fixture) -> None:
    source, middle = f.crawls[-1], f.crawls[-2]
    dead = f.add_page(status=404, purpose="retired_resource")
    source.update({"final_url": dead["url"], "status_code": 404, "canonical": None,
                   "indexability": "blocked", "redirect_chain": [source["url"], middle["url"], dead["url"]]})
    _body(source, "The requested document was not found.")
    middle.update({"final_url": dead["url"], "status_code": 404, "canonical": None,
                   "indexability": "blocked", "redirect_chain": [middle["url"], dead["url"]]})
    _body(middle, "The requested document was not found.")
    f.exclude_index(middle, "permanent_redirect_alias")
    f.unlink(middle["url"])
    f.exclude_index(source, "permanent_redirect_alias")
    f.context["sitemap_urls"].append(source["url"])
    for page in f.crawls:
        if page["url"] != f.urls[0]:
            page["links"] = [u for u in page["links"] if u != source["url"]]
    f.crawls[0]["links"].append(source["url"])
    f.unit("redirect_chain", source, related=[middle["url"], dead["url"]], reason="The chain has multiple hops even though its terminal response is unavailable.")
    f.unit("broken_internal_link", f.crawls[0], related=[source["url"]], related_mode="contains",
           reason="Following the current internal reference terminates at an observed unavailable resource.")
    f.unit("sitemap_unavailable_url", source, reason="A current sitemap URL resolves through redirects to an unavailable resource.")


def _mixed_alias(f: _Fixture) -> None:
    alias, primary, mistaken = f.crawls[-1], f.crawls[1], f.crawls[2]
    alias["canonical"] = primary["url"]
    _body(alias, primary["main_text"], primary["main_heading"])
    f.exclude_index(alias, "printable_version")
    f.protected_urls.append(alias["url"])
    mistaken["canonical"] = alias["url"]
    f.unit("canonical_chain", mistaken, related=[alias["url"], primary["url"]],
           reason="A different original document canonicals through a printable alias; the intentional alias itself should not be 'fixed' to self-canonical.")
    if f.variant % 2:
        mistaken["robots_directives"], mistaken["indexability"] = ["noindex"], "blocked"
        f.unit("indexability_review", mistaken, reason="The same original document also contradicts its public indexing intent.")


CONTROLS: tuple[tuple[str, Callable[[_Fixture], None]], ...] = (
    ("clean_complete_graph", _clean_dense), ("legitimate_small_contextual_graph", _small_contextual_graph),
    ("intentional_print_canonical", _intentional_print), ("separate_language_intents", _language_intents),
    ("intentional_utility_noindex", _utility_noindex), ("intentional_private_robots", _private_robots),
    ("intentional_retired_resource", _retired_resource), ("legitimate_one_hop_migration", _one_hop),
    ("similar_topic_different_intent", _different_intent), ("functional_short_interactive_page", _interactive_short),
    ("complete_concise_definition", _short_definition), ("genuine_article_about_404", _real_404_guide),
    ("complete_rendered_application", _rendered_complete), ("safe_sitemap_exclusions", _sitemap_exclusions),
    ("equivalent_canonical_authority", _equivalent_canonical), ("benign_quoted_schema", _benign_schema),
    ("multiple_legitimate_entrypoints", _multiple_entrypoints), ("fragment_normalised_links", _fragment_links),
)
FAULTS: tuple[tuple[str, Callable[[_Fixture], None]], ...] = (
    ("observed_broken_destinations", _broken_links), ("complete_inventory_true_orphan", _orphan),
    ("canonical_to_missing_target", _canonical_missing), ("direct_canonical_cycle", _canonical_cycle),
    ("unexplained_external_canonical", _canonical_external), ("noindex_conflicts_with_owner_intent", _accidental_noindex),
    ("stale_sitemap_unavailable_url", _stale_sitemap), ("redirect_paths_chain_or_loop", _redirect_chain),
    ("duplicate_metadata_distinct_documents", _duplicate_metadata), ("success_status_missing_resource_body", _soft_404),
    ("unfinished_article_placeholder", _placeholder), ("robots_conflicts_with_owner_intent", _robots_conflict),
)
AMBIGUOUS: tuple[tuple[str, Callable[[_Fixture], None]], ...] = (
    ("incomplete_graph_apparent_orphan", _partial_graph), ("timeout_not_terminal_failure", _timeout),
    ("unavailable_sitemap_not_empty", _sitemap_unknown), ("unobserved_rendered_content", _render_unavailable),
    ("overlap_without_query_evidence", _overlap_unknown), ("unobserved_canonical_destination", _canonical_unseen),
)
HOLDOUT_INTERACTIONS: tuple[tuple[str, Callable[[_Fixture], None]], ...] = (
    ("canonical_into_noindex_target", _canonical_noindex), ("robots_and_sitemap_crossed_faults", _sitemap_robots),
    ("canonical_cycle_through_redirect", _canonical_redirect_cycle), ("broken_bridge_orphan_component", _broken_bridge),
    ("rendered_failure_overrides_raw_article", _rendered_failure), ("thin_overlap_and_indexability", _thin_overlap),
    ("sitemap_redirect_chain_to_missing", _redirect_dead), ("legitimate_alias_inside_faulty_chain", _mixed_alias),
)


def build_corpus(split: str, seed: int = 20260903) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build unlabeled runtime cases and isolated evaluator truth.

    No runtime module is imported.  Holdout variants use different cardinalities
    and navigation topologies; eight entire interaction families are held out.
    A seed rotation creates a new export, not independent statistical evidence.
    """
    if split not in {"development", "holdout"}:
        raise ValueError("split must be development or holdout")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    records: list[dict[str, Any]] = []
    private: dict[str, Any] = {}
    groups = [(CONTROLS, "control"), (FAULTS, "definite"), (AMBIGUOUS, "ambiguous")]
    if split == "holdout":
        groups.append((HOLDOUT_INTERACTIONS, "compound"))
    for recipes, stratum in groups:
        variants = (2, 3, 4) if split == "holdout" else ((0, 1) if stratum == "definite" else (0,))
        for family, recipe in recipes:
            for variant in variants:
                fixture = _Fixture(family=family, variant=variant, seed=seed, stratum=stratum)
                recipe(fixture)
                runtime, truth = fixture.export()
                records.append(runtime)
                private[runtime["case_id"]] = truth
    random.Random(f"{seed}:{split}:order").shuffle(records)
    truth = {
        "schema_version": SCHEMA_VERSION, "split": split, "seed": seed,
        "runtime_input_sha256": _digest(records), "cases": private,
        "counts": {
            "cases": len(records), "families": len({v["family"] for v in private.values()}),
            "issue_units": sum(len(v["units"]) for v in private.values()), "decision_units": len(records),
            "strong_no_action_cases": sum(v["stratum"] == "control" for v in private.values()),
            "ambiguous_cases": sum(v["stratum"] == "ambiguous" for v in private.values()),
            "holdout_only_interaction_cases": sum(v["stratum"] == "compound" for v in private.values()),
            "simulated_rendered_cases": sum(v["rendered_evidence"] != "none" for v in private.values()),
        },
        "scope": "Structural diagnosis from synthetic observations; not live model competence, Google indexing, causal business uplift, or real rendering.",
        "autonomy_level": 1, "production_enabled": False, "production_write_budget": 0,
        "paid_api_calls": 0, "level_2_eligible": False,
    }
    truth["truth_commitment_sha256"] = _digest(truth)
    return records, truth
