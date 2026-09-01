# Capability and source ledger

Checked for this build on **2026-09-01**. Exact installed package versions are in `requirements.lock.txt`. Current API documentation is a capability source, not evidence of this site's SEO outcomes.

| Area | Official source | Material implementation consequence |
| --- | --- | --- |
| Agents orchestration | [OpenAI orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration), [running agents](https://developers.openai.com/api/docs/guides/agents/running-agents) | Manager-oriented code, typed output and bounded calls; SDK availability is not a scheduler or database |
| Work / custom MCP | [MCP server development](https://developers.openai.com/plugins/build/mcp-server), [developer mode](https://developers.openai.com/api/docs/guides/developer-mode), [authentication](https://developers.openai.com/plugins/build/auth) | Local stdio supported; remote HTTPS/OAuth and connector registration require actual host/account setup |
| GSC | [Search Analytics query](https://developers.google.com/webmaster-tools/v1/searchanalytics/query) | Final-data requests, pagination, privacy/top-row omissions and Pacific dates; separate page totals |
| GA4 | [runReport](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport), [schema](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema), [checkCompatibility](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/checkCompatibility) | Explicit organic session scope and qualified event mapping; compatibility and metadata quality checks |
| GA4 processing | [Data freshness](https://support.google.com/analytics/answer/11198161?hl=en) | Processing/attribution can change; a local 12-day holdback is conservative policy, not guaranteed finality |
| WordPress | [Posts reference](https://developer.wordpress.org/rest-api/reference/posts/), [authentication](https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/) | Scoped application passwords, edit-context snapshots and draft path; ordinary REST update lacks the required atomic revision contract |
| Traditional SERP | [DataForSEO organic live advanced](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/) | Explicit paid opt-in, bounded depth/locale, one attempt to avoid duplicate charges |
| AI search | [DataForSEO AI Mode](https://docs.dataforseo.com/v3/serp-google-ai_mode-live-advanced/), [Google AI features](https://developers.google.com/search/docs/appearance/ai-features) | Provider sample only; no invented universal citation metric or GSC AI-only report |
| Search policy | [Google spam policies](https://developers.google.com/search/docs/essentials/spam-policies) | No scaled low-value content or fabricated claims, regardless of generation method |
| Containers / privileges | [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/), [PostgreSQL grants](https://www.postgresql.org/docs/17/sql-grant.html) | Health/migration dependencies and separate owner/runtime roles |

## Observed environment

Python, shell, Git, connected GitHub read capabilities, MCP tooling, code execution and persistent deliverable storage were available. A local branch was created. No suitable new GitHub destination was selected; the exposed unrelated repository was left untouched. No provider credentials, production domain or deployment host were supplied.

Docker/PostgreSQL execution was unavailable locally. Browser automation code was available, but a Chromium runtime could not be downloaded through the available network; local browser rendering is therefore unverified. The delivered CI workflow includes those external gates. No paid provider call, real CMS mutation, remote deployment or Work connector activation is claimed.

The build checkpoint survives independently of conversation through committed source, its recovery state and saved source archive. Continuous live operation begins only on persistent user-selected infrastructure.
