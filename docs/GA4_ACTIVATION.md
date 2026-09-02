# GA4 activation checkpoint — 2026-09-02

The dedicated SEO Test Lab web stream exists for `https://seo-test-lab.pages.dev`, with public Measurement ID `G-5CZFWBNRC3`. Enhanced measurement is persisted off. All four optional account data-sharing settings and all five optional email preferences were observed off. No Google account settings were submitted by the agent during this readback.

The tag remains off until a visitor selects **Allow test analytics**. Advertising consent is denied and Google signals/personalisation are disabled in the reviewed client. The checklist event has no monetary value and must never be mapped to qualified business conversions. Google may set cookies after consent; reloading returns the lab's consent gate to off but is not a cookie-deletion operation.

## Exact release

- Site PR: [seo-test-lab #4](https://github.com/DARKLORDVINAY/seo-test-lab/pull/4).
- Site merge: `01608070c2ed22de636a703a673ed4da46a00a9c`.
- Site tree: `4e675d59c80bca30b51467d01b237347fd42804d`.
- Inventory SHA-256: `5596ab79b4c5b96a4729b281bccc274b17b59ffba930f79c32291711460031aa`.
- Successful Pages deployment: `bc6bc78a-af6d-4c86-a565-fd362f232c0c`.
- Operational experiment: `af5d2a12-68f9-433e-ac21-62257b255ab6`.

Independent delta review passed 20/20 checks. It compared all 33 release files with the previously observed baseline: only 27 HTML consent/ID additions, security-header allowlists and inventory hashes changed. Page content, canonical/robots directives, links, JavaScript, CSS, robots.txt, sitemap and evaluator ground truth are unchanged. The earlier 39-test source/VM consent review was reused, not relabelled as a new live test.

Run the existing public-test-lab-shadow workflow against the updated `public_target.json` pin to assess the deployed revision. Its public browser gate deliberately never grants consent and blocks third-party requests. It cannot establish Google receipt. The baseline structural figures in the dated verification JSON remain historical; retain new run logs and operator evidence separately.

## Owner-only completion

In the existing property, use **Admin → Data display → Events → Create event**:

1. Event name: `lab_checklist_complete`.
2. Mark as a key event for this test property only.
3. Choose **Create with code**. The site already implements this exact event; do not create a URL/page-view-derived substitute.
4. Do not assign a default monetary value. Keep the default once-per-event counting.
5. Review and save using the owner browser handoff or an explicit action-time approval.

Then use [the practice exercise](https://seo-test-lab.pages.dev/exercises/), explicitly opt in to test analytics, complete all three checks, and record one practice completion. A queued event is not proof that Google received it. Confirm the exact event in Realtime/DebugView before reporting delivery; verify reporting API access separately.

No event receipt, completed key-event registration, backend API credentials, durable hosted scheduler, live model run, commercial outcome, or Level 2 graduation is claimed by this checkpoint.

## Browser continuity and recovery

The old setup tabs showed stale no-stream/verification screens after the owner finished setup. A fresh page for the already-created property showed the saved stream and privacy choices. Never create another account or stream merely because an old tab has reset.

For a genuine owner-only browser gate, use the current verified task tab, request one supported manual handoff, and end the assistant turn immediately so the takeover control can appear. Agent-side rendering does not prove that the user's panel responds. If it is blank, supply a verified ordinary link for completing persistent account settings independently; logging into a separate browser does not authenticate the cloud session.

## Rollback and authority

Restore the baseline served assets from tree `962816b3497c193b0d161dfc60e301a92e80fabc` through a new reviewed PR, preserving unrelated repository files. Verify public hashes/directives and repin the observer. The prior public A→B→A drill established the release/restore mechanism.

Rolling back this tag stops activation on newly loaded baseline pages. It does not unload already-running Google code, erase existing cookies, or retract transmitted events. Such data operations require their own owner decision.

Retain Level 1, `PRODUCTION_ENABLED=false`, shadow mode, zero earned categories, and a zero autonomous site-write budget. Manual, explicitly requested lab deployment is audited separately from the disabled autonomous executor.
