# Editor Mode as a self-service service: gap analysis

What Editor Mode already does, and what is missing, to run as a service where
anyone signs up, enters a domain and gets an AI newsroom of their own. Read
from the code, not from the README. Base: 0.4.0, then the "Unreleased"
section of the [CHANGELOG](../CHANGELOG.md).

**Present**, **partial**, **missing**; *fixed* marks what changed after 0.4.0.

## Newsroom

| Need | State | Where |
|---|---|---|
| Domain → site scan (robots, sitemap, feeds, categories, article sample) | Present | `site.py`, through the Core's fetch |
| Sector, subtopics, language, audience, style, with evidence | Present | `profile.py`: deterministic baseline, the model may cite only pages read |
| Profile versioned, editable, approved by the owner | Present | `editorial_profiles` |
| Periodic re-analysis and proposed new line, never applied alone | Present | weekly review, drift |
| Partly covered topics (content gaps) | Missing | |
| Confidence per field | Partial | incomplete analyses are declared, no score per field |
| Dynamic source discovery and rating | Present | `sources.py` |
| Hype and Editorial Opportunity, separate scores | Present | `scoring.py`, `opportunity.py` |
| Hype signals outside tech | Partial | collectors: RSS, Hacker News, GitHub, Hugging Face, arXiv |
| Strong hype → proposal and draft at once | Missing | proposals are made at the morning briefing, drafts on request |
| Thresholds and hysteresis per tenant | Partial | fixed thresholds, MinHash dedup |
| Evergreen topics, updating an existing article | Missing | |
| Draft: every claim with its sources, numbers checked against them | Present | `drafts.py` |
| Contradictions between sources, originality beyond copy checks | Partial | |
| SEO: slug, meta description, category, internal links | Missing | |
| Preferred and excluded topics as rules | Missing | only by editing the profile |
| Editorial comments on drafts | Partial | versions yes, comments no |

## Publishing

| Need | State | Where |
|---|---|---|
| Human approval by default, explicit states | Present | draft `needs_review → approved/rejected`, publication `pending_approval → approved → sending → published/failed` |
| `scheduled` state | Missing | |
| Only targets the tenant configured | Present | `publish_targets`, row-level security |
| No double publication | Present | compare-and-set `approved → sending` |
| WordPress, webhook, Telegram channel | Present | pinned client, no redirects |
| Credentials entered by the customer | *Fixed* | encrypted in the database, entered from the dashboard (`vault.py`) |
| Proof of control of the domain before publishing | Missing | |

## Running many tenants

| Need | State | Where |
|---|---|---|
| Isolation between tenants | Present | row-level security, `test_tenancy.py` |
| Durable jobs, nothing lost on restart | *Fixed* | `editor.jobs` queue, `queue.py` |
| Fair scheduling | *Fixed* | one job per tenant, least recently served first, `--concurrency` |
| A Core tenant per newsroom, created without restart | *Fixed* | delegating token, `X-EuLLM-Tenant`, limits through `PUT /v1/tenants/{name}/limits` |
| Model budget reserved before the call | *Fixed* | the Core reserves each call on the newsroom's own budget (the plan's `--max-cost-month`) |
| Limits on calls in flight shared between Core processes | Missing | per process today |
| Shutdown that waits for runs (Core) | Missing | the Editor worker already waits |
| Export and deletion of a tenant's data | *Fixed* | `tenant_data.py`, `editor tenant-export`, `tenant-delete`, download from Impostazioni |
| Retention | *Fixed* for Editor Mode | `EDITOR_RETENTION_DAYS`; Core audit and usage still kept for ever |
| Cost per newsroom | Partial | model usage per tenant; web search and retries not counted |

## Self-service and billing

| Need | State |
|---|---|
| Users with verified email, invitations | Missing: dashboard login is a token made from the command line |
| Tenant created at sign-up | Missing: `editor tenant-add` |
| Onboarding wizard: domain → profile → approval | Partial: the steps exist in the dashboard, not as a guided flow |
| Plan catalogue with quotas | Partial: limits per tenant (`tenant-plan`), no catalogue |
| Payments, trial with a fixed budget | Missing |

## Later

Social posts (LinkedIn, X, Facebook first), instructions in natural language
turned into settings: missing, by design after the above.

## Order

1. **Road test** on real sites with a real model: the newsroom has been
   tested only against fake sites.
2. **Multi-tenant consolidation**: the *fixed* rows above.
3. **Editorial autonomy**: strong hype, thresholds, evergreen, topic rules,
   SEO, `scheduled`, collectors for other sectors.
4. **Self-service**: users, sign-up, wizard, proof of domain control.
5. **Commercial**: plan catalogue, payments, trial, a repeatable benchmark
   with many tenants.
6. **Extensions**: social, natural-language settings.
