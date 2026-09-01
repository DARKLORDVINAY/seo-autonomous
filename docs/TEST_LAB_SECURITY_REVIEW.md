# Controlled Test Lab: independent adversarial review

Date: 2026-09-01. Scope: the static release boundary, crawler/detector changes, `services/test_lab.py`, artifact rollback, and the browser-check script. This review did not operate external accounts or deploy a website.

## Assessment

The reviewed code is suitable for the controlled Level 1 demonstration with autonomous production writes disabled. Two additional benchmark-scoring defects were reproduced and fixed before this assessment. No uncontrolled production-write path was found in the reviewed changes.

The corrected evaluator passes the existing frozen artifact packet: **13 TP, 0 FP, 0 FN, 13 correct NO-ACTION decisions**, with all required high/critical policy probes blocked and no recorded autonomous production changes. This is an artifact development result, not evidence of live search performance, public rollback, model reasoning quality, or Level 2 eligibility.

## Findings and fixes

| Finding | Verified boundary or correction |
| --- | --- |
| An overlap label could count as a true positive without checking any of its factual facets. | Reproduced with unrelated Physics/Cooking pages. The evaluator now checks bounded observed word-sequence overlap, shared heading terms, and distinct title/description values. Missing, trivial, or oversized comparison evidence fails closed. Matching search intent remains explicitly unverified. |
| NO-ACTION on the second affected member of a duplicate/overlap pair escaped the error count. | Reproduced for all three pair categories. Both affected members now count; clean canonical destinations and other related evidence pages remain eligible for NO-ACTION. |
| Empty or partial high/critical probe lists could imply success. | The gate requires every expected action kind exactly once, with `allowed` explicitly false. Existing regressions pass. These are policy probes, not live destructive attempts. |
| A correct detection plus a false primary-page NO-ACTION could pass. | The gate rejects false NO-ACTION decisions. Incomplete controls cannot silently count as correct decisions. |
| A 429, blocked fetch, or truncated link graph could support a false completeness assertion. | Collector attestation and generic graph checks refuse complete coverage. Failed current crawls cannot borrow cached page observations from an older release. |
| A raw noindex directive could be mislabeled accidental. | Accidental exclusion requires separately attested owner indexing intent. A 404 is not relabeled as accidental noindex. |
| Related evidence could falsely implicate a correct canonical target. | Affected-page selection distinguishes the faulty source from supporting destinations. |
| An incidental button could make a thin informational page appear exempt. | Purpose comes from the attested inventory; guide/note/reference diagnostics cannot be bypassed by adding a button. |
| A symlinked parent could place a rollback copy inside the original release. | The destination is resolved before containment checks and before copying. Existing destinations, source symlinks, hidden assets, excessive assets, and source/label manifests are rejected. |
| Hostile manifest routes could escape the browser target. | Directory-route validation rejects origin changes, traversal, credentials, encoded separators, queries, and backslashes. Browser requests are restricted to the selected origin. |
| A shadow status string could hide an external dispatch. | The frozen packet cross-checks immutable action events; a dispatch record makes the no-write gate fail. |
| Webpage instructions could obtain publication authority. | Crawled text remains untrusted data. The existing model test deliberately returns an injected publish recommendation; agents have no executor tools/handoffs and deterministic policy blocks the recommendation. |

The new overlap check uses a word-sequence comparison rather than calling the detector's four-gram scorer. Its bounded near-copy threshold is a benchmark premise check, not a universal content-quality rule or a claim of causal cannibalisation.

## Verification performed

- **50 targeted tests passed** across lab review, benchmark, detectors, rollback, and crawler/model/analytics prompt-injection regressions. Nine review cases were added; six reproduced failures before the fixes.
- Ruff passed for the two changed Python modules.
- Independent Node checks rejected seven unsafe public origins and five unsafe inventory routes, while accepting a valid 26-page inventory. This checks input boundaries; it is not a rendered-browser result.
- Read-only reassessment of canonical frozen decision evidence `8a5e1b89-5e5d-4c95-b72f-6eefd0aa3b8e` passed. Its packet SHA-256 is `8c7c17c254a2f341601a8eba9f75f5b58d4eba7b94045688ac6a8e71269b0ce3`.
- The ground-truth bytes remain unchanged: SHA-256 `924d52d55671481cd51ea37ead6ef36d46dc7bd99c113c77c156be83d1a1418d`. Public content was not changed to make the evaluator pass.

## Residual uncertainty and release gates

The builder serves only generated release assets; it does not read or copy the evaluator labels. The decision packet is committed before label evaluation, and benchmark failure labels are excluded from specialist inputs. A public source repository can nevertheless expose those labels to a human or another system. This is runtime separation, **not a secret or independent holdout**; the benchmark categories informed development.

Owner judgments such as whether a page meets its promised need or whether two exercises serve the same task remain judgments. The report lists unchecked facets. Lexical overlap, low word count, and sitemap differences justify investigation only. Actual query cannibalisation, indexing, useful conversions, and causal value require separate observed evidence.

Public serving can change headers, redirects, status codes, or asset delivery. The stable production URL therefore needs its own attested crawl and browser check. The artifact Git restore proves restoration of the copied release, not Cloudflare deployment rollback. Public rollback, PostgreSQL/container operation, Google account/API access, GA4 event receipt, and live-model/outcome evaluation require their respective evidence. Operator deployment auditing added outside this review needs its own verification.

No review result or structural score changes the global/site write switches, earns an action category, substitutes for calibration, or authorizes autonomy graduation.
