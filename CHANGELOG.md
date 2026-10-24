# Changelog

## 0.17.3

### Fixed

- Editor Mode on the server: every read of the shared archive and every
  model call made for it (the hourly collection, the topic names) was
  refused by the Core, which takes tenant names that start with a letter
  or a digit, not `_archive`. Sites whose sources were read only by the
  archive (eullm.eu, rag-pro.com) never got a news item, the others only
  from collections started by hand, and topics kept names made of bare
  keywords. The archive is now `shared.archive` for the Core. This was
  also what suspended 46 sources this morning.

### Changed

- Editor Mode, Trend e piano: the topic map is four boxes (Scrivi ora,
  Nicchia utile, Rumore, Ignora) listing every topic of the week with its
  Opportunity and Hype, instead of dots whose labels overlapped.

## 0.17.2

### Changed

- Editor Mode, richer articles: an article is at least 900 words (up to
  2000, the site's usual length in between; before it could be 400), with
  up to 12 more news items on the same subject as context, claims of one
  to three sentences and the details the items carry. A draft still far
  under its length is sent back once with the items it left out, and the
  longer version is kept only if it passes every check (Attività shows
  the step). The prompt now asks for a journalist's prose: varied
  sentences, no stock phrases, no em dashes, no closing summaries.

## 0.17.1

### Fixed

- Editor Mode: good feeds were suspended for everyone after five failed
  reads, which with hourly reads is five hours: a site down for an
  afternoon, or the server's own network, stopped TechCrunch, arXiv, Hacker
  News and the like for two weeks. Now an archive source is suspended after
  24 failed reads in a row (about a day) and a source suspended for failed
  reads is tried again every day, not after two weeks. When more than half
  of the sources fail in the same round the trouble is on this side (the
  server's network, DNS, the Core), so the failures are shown but do not
  count towards suspension, and Attività says so. Sources suspended by the
  old rule go back to work by themselves at the next collection.
- Editor Mode: "Accetta e scrivi la bozza" answered Internal Server Error,
  and so did every page with the Attività box while a draft was being
  written (the box read the proposals' sites the wrong way).


## 0.17.0

### Added

- Editor Mode, cover images. Each draft gets an image drawn for it by FLUX
  (Black Forest Labs, on its EU endpoint `api.eu.bfl.ai`), never taken
  from the news it comes from: the model writes the image prompt and the
  alternative text in the article's language (one short call), FLUX draws
  it (16:9, about one megapixel) and the image is kept in the database,
  since the provider deletes it after ten minutes. The draft page shows it
  with "Rifai l'immagine" and "Togli"; translations take the original's
  image with their own alternative text. At publication WordPress receives
  it in the media library, with its alternative text, as the post's
  featured image; webhooks get `image.url` (served at `/media/<token>`,
  without login, by an unguessable token). Each image is in Consumi with
  its cost (Black Forest Labs' credits, one cent each). Set
  `EDITOR_IMAGES_API_KEY` (and optionally `EDITOR_IMAGES_MODEL`, default
  `flux-2-pro`; `flux-2-klein-9b` costs less): without a key drafts have
  no image, as before.
- Consumi names the steps of translations and images.
- Editor Mode, delivery of the approved articles, for the customers whose
  site is not connected to a destination. The draft page has a "Consegna"
  card: "Scarica il pacchetto" gives a ZIP with the page ready to open
  (`article.html`, with the search fields, the schema.org data and the
  image), the text alone to paste into any CMS (`body.html`), Markdown
  with front matter for Hugo, Jekyll and Astro (`article.md`), the cover
  image and every field for Google (`meta.json`); "Copia HTML" and "Copia
  Markdown" copy the text without its title. At each approval the
  newsroom's email recipients (Impostazioni) get the article in the
  message with the package attached, a job of its own shown in Attività
  (needs SMTP). In Sito e profilo › Come ricevi gli articoli the owner
  creates the site's feed, JSON Feed 1.1 and RSS 2.0, with the last 50
  approved articles complete with text, image and search fields
  (`?lang=en` for one language), for Zapier, Make, n8n or a site that
  imports a feed: its address holds an unguessable token, needs no login,
  and can be changed. API: `GET /api/drafts/{id}/package` and
  `POST /api/sites/{id}/feed`.

## 0.16.0

### Added

- Editor Mode, translations. In Sito e profilo › Lingue degli articoli the
  owner picks the languages a site's articles are translated into
  (English, Italian, German, French, Spanish, Portuguese, Dutch, Polish),
  within the plan's languages per site (`max_languages`, its own included;
  now on the newsroom too, copied from the plan, set in /admin and with
  `editor tenant-plan --max-languages`). When an article is approved the
  agent translates it into each of them, one model call per language: a
  translation is a draft of its own with the same sentences in the same
  order and the same sources, checked like the original (numbers found in
  the sources, nothing copied), with its own search title, description and
  slug, approved and published separately. Each translation counts as one
  article of the plan. A draft page lists the original and its
  translations, with "Traduci" for a language still missing. API:
  `PUT /api/sites/{id}/languages`, `POST /api/drafts/{id}/translations`;
  approving a draft through the API answers with the languages being
  translated.

## 0.15.0

### Added

- Editor Mode, the news base: 46 general news feeds read once an hour in the
  shared archive for every newsroom, whatever its sites follow: world and
  politics, economy and technology, in English (BBC, Guardian, NYT, Al
  Jazeera, DW, France 24, Euronews, FT, Ars Technica, The Verge, TechCrunch,
  Hacker News...) and in Italian, German, French and Spanish (ANSA,
  Corriere, Repubblica, Il Sole 24 Ore, tagesschau, Spiegel, heise, Le
  Monde, El País, Xataka...), so national news reach the sites of that
  country. `EDITOR_BASE_NEWS=0` turns them off.
- News index (/index): every three hours from 07:00, the 40 topics rising
  most across all sources, ranked, each with a short summary, its news,
  how many sources tell it and its section (world, economy, technology, or
  the sites' own sources), with tabs per section and "Titoli per <sito>".

### Changed

- The keywords of a site's profile are written in the site's language and
  again in English, so both national and international news are found.
  Existing profiles get them at the next analysis ("Rianalizza").
- Topic labels by the model are capped at the 10 largest topics touched by
  each run, so the larger base does not multiply the cost; the others keep
  their keyword labels until they grow.

### Fixed

- Trend e piano showed "<built-in method items>" instead of the number of
  news of a topic.

## 0.14.0

### Changed

- Editor Mode, proposals every day. Until now a proposal needed a topic
  told by two sources, or one "strong" news item, and its closeness to the
  site was a word-for-word match between the profile's keywords, in the
  site's language, and news that come mostly in English: Italian sites got
  "nothing to propose" in 0 seconds. Now the topics come first, as before,
  and the rest of the day's number comes from the best single news items of
  the last 72 hours for the site (its own sources first, then the shared
  archive): scored without the model on word roots (so "inferenza" meets
  "inference"), the site's sources and their rating, freshness, how many
  sources tell the story and what the site already covered. One model call
  reads the first 30 and picks the best, writing title and angle (before:
  one call per proposal); without the model the formula's first ones are
  taken. Each such proposal is marked "notizia singola" and says why it was
  chosen. Nothing is proposed only when no news came in for 72 hours, and
  the Attività box says so in red.
- Desk, a cockpit of the whole newsroom: six figures at the top (decisions,
  proposals, drafts, news in 24 hours, sources, model cost over 30 days),
  decisions one line each with the details on click, the Attività box one
  line per job with the full day on the new Attività page (/activity), the
  sites table with each site's model cost over 30 days, and the most active
  sites, the most expensive sites and the latest news.

### Added

- `proposals.basis`: what a proposal stands on (a topic, or a single news
  item with its score and who picked it). Migration 0023 (SQLite 0012).

## 0.13.1

### Added

- Server compose: the Core receives `ANTHROPIC_API_KEY`, for a Claude model
  (`type: anthropic` in core.yaml).

### Fixed

- Console, "Consumi" page: the "Esci" (log out) button was missing.

## 0.13.0

### Added

- Editor Mode, model usage measured call by call. Every model call is
  recorded when it happens, also when the work then fails: newsroom, site,
  article (the proposal or topic it was for), step, the model that
  answered, input and output tokens, duration, the job it ran in and, for
  a failed call, the error. Before, calls were recorded only at the end of
  a successful step, without site or article.
- Console, "Consumi" page (/admin/usage): tokens, calls, errors, average
  duration and cost per step, model, newsroom and site over 1, 7, 30 or 90
  days; the cost of every article written (writing attempts and the
  proposal of its topic) and the average per article; every call as CSV.
- Reference prices per model (euro per million input and output tokens),
  set in the console: the cost of an article is estimated also when the
  model is free today.

### Fixed

- A newsroom with a timezone the system does not know no longer breaks the
  console: it counts as UTC.

### Upgrading

- Migration 0022 (Postgres) / 0011 (SQLite) adds the new columns of
  `llm_usage` and the `model_prices` table; it runs with the update.

## 0.12.3

### Fixed

- Editor Mode, site page: two separate statuses instead of one that looked
  contradictory. "Lettura del sito" says how much of the site was read
  (articles, dates, language); "Profilo" says whether the model worked out
  sector, audience and style. Before, a site could show "Analisi completa"
  with a partial profile, or "Analisi parziale" with a complete one.
- A model provider that fails for a moment (5xx, rate limit, timeout) is
  asked again after 15 and 45 seconds, for every model call (profile,
  proposals, titles, drafts, sources); Activity says so. Refusals such as a
  spending limit or an invalid answer are not retried.
- Work stopped by a model provider that stays down (a draft, for example)
  is no longer marked failed: it waits and runs again by itself after 10,
  20, 30... minutes, up to 8 attempts, and Activity shows when.
- Editor Mode waits up to 15 minutes for the Core (`EDITOR_CORE_TIMEOUT`,
  in seconds) instead of 3: a model running on the same machine, or a
  spare model tried after the first, has the time to answer.
- A profile left incomplete because the model did not answer is analysed
  again by the hourly tick, waiting longer each time (up to 6 times a
  day), with no button to press. A review of an approved site where the
  model does not answer keeps the approved line and proposes nothing,
  instead of a version without sector, audience and style.

## 0.12.2

### Added

- Editor Mode: whatever is created from the dashboard can now be corrected
  or deleted from it, without a command line.
  - Sites: rename; correct a mistyped domain until a profile is approved
    (the site is analysed again under the new domain); delete a site with
    everything that belongs to it, typing its domain to confirm. Work queued
    for it is dropped; a site with work running is deleted when it ends.
  - Publishing targets: edit name, address, WordPress user, IndexNow key,
    JSON-LD and on/off; delete a target (articles already on the site stay).
  - Briefing recipients: edit; newsroom preferences (timezone, briefing
    hour, language) set by the owner in Impostazioni.
  - Drafts: edit title, subtitle, every sentence and the SEO fields; claims
    are checked again and an approved draft goes back to review. Delete a
    draft; its proposal can be written again.
- Console: rename a newsroom; edit and delete customers (not while they
  have a newsroom) and resellers (not while they have customers); add and
  delete plans (not the trial plan, not a plan in use); remove an operator
  (never yourself, never the last one); revoke an operator invite.
- Translations of the new screens in English, German, French and Spanish.

## 0.12.1

### Fixed

- Editor Mode: a site added from the dashboard is listed at once, in the
  sidebar and on the Desk, while its analysis waits or runs; before it
  appeared only when the analysis ended.
- Activity: an analysis that could not read the site shows as failed, in
  red, with the reason (for example a domain that does not answer, not even
  robots.txt), instead of "Done". A profile the model could not complete
  says so ("Da completare") and why; a search for sources before the
  profile is approved says that sources come after the approval.
- A newsroom runs two jobs at a time instead of one, so a second site's
  analysis no longer waits minutes behind the first one's model step; the
  same work on the same subject, and two collections, never run together.
  The model step says it usually takes 1-3 minutes, and it is skipped when
  the site could not be read.

## 0.12.0

### Added

- Editor Mode, the Desk (it replaces "Oggi"): every site on one page. A
  table gives each site its profile, active sources, news in the last 24
  hours, proposals and drafts waiting, the next step it needs and buttons
  to analyse it, find sources and propose articles; the collection runs
  for all sites at once. Decisions of every site are listed together.
- Activity: every job started from the dashboard shows at once, queued,
  then running with the step it is on ("Lettura degli articoli del sito:
  7 di 25", "Lettura delle fonti: 12 di 40", "Il modello scrive la
  bozza"), then what it produced with a link to it: a profile to approve
  or "no change", sources tried and activated, news and topics read,
  proposals made, the draft written. A failed job says why and at which
  step. The box refreshes by itself on every page and the page reloads
  when the work ends; if work waits more than three minutes with nothing
  running, it says the worker seems stopped.
- When no proposal comes out, the result names the topic closest to one,
  with its Opportunity and its news and sources. An accepted proposal
  shows whether its draft is being written or failed, and why.
- Jobs keep their step and their result (`editor.jobs.progress`,
  `editor.jobs.result`, migration 0020).

## 0.11.0

### Added

- Editor Mode, articles written for search engines and AI answers: the
  first lines answer the reader's main question; the headings are the
  questions people search for; two to four frequently asked questions
  close the article, each answered with sourced claims checked like the
  rest; a key phrase is chosen and used in the title and the first
  sentence; a "Read more" section links up to three of the site's own
  articles on the same subject.
- Every draft carries its search fields (key phrase, a title for Google
  within 60 characters, a description within 160, the address words)
  and a checklist on its page, "Google and AI answers", that says what is
  in place and what is missing.
- Publishing: WordPress gets the address words and the description as the
  excerpt; with the new option, the post also carries schema.org JSON-LD
  (NewsArticle with its sources, FAQPage) that says what the page says.
  The webhook sends the same fields. With an IndexNow key on the target,
  every published article is reported at once to Bing and the engines
  that share IndexNow (the key file goes at the site's root); a failed
  report is logged and the article stays published. Google has no such
  call for articles: it finds them through the sitemap and internal links.

## 0.10.1

### Added

- Editor Mode: "Lancia adesso" on the home page runs every step on
  request, without waiting for the schedule: analyse the site, find
  sources, collect the news (then topics, Hype, a fresh trend of the
  moment and Opportunity) and propose articles. Proposals asked for by a
  person are no longer held back by the day's number; the automatic ones
  still are.

### Changed

- CI: main is not checked again after a merge (the branch merged was
  checked by its own push); only the release cache is built there. The
  release page is created as soon as the tag is pushed and each platform
  adds its files when its build ends, so a queued Windows or macOS runner
  holds up nothing. Every job has a time limit: a hung step fails in
  minutes instead of running for hours.

## 0.10.0

### Added

- Editor Mode, shared archive: every public source is read once for all
  newsrooms, not once per newsroom. Items, topics, topic labels and Hype
  Scores live in a reserved tenant, `_archive`, that newsrooms can read
  but not change (row level security, read-only policies); the hourly
  collection reads only the sources at least one newsroom follows. Each
  newsroom keeps its own choices: which sources each site follows and
  their status, the site's rating of each source, Opportunity, proposals,
  drafts and feedback. A newsroom that starts following a source already
  in the archive sees its history at once. A source that stops answering
  is suspended in the archive for everyone; a source off topic is
  suspended only for the site that follows it. The archive's pruning
  keeps every item a newsroom cites. On SQLite (local installation) the
  only newsroom is its own archive. The migration moves existing sources
  into the archive without duplicates and keeps them followed; items and
  topics collected so far stay where they are and age out.
- The trend of the day: from 07:00, every three hours (eight times a
  day), the archive keeps the topics rising most with their latest news.
  "Trend e piano" opens with the trend of the moment for the site: the
  rising topics close to its profile, their sources and news, and the
  titles proposed for each, with a short summary and "Scrivi questo" to
  have the draft written. "Proponimi tre titoli" asks the model for three
  different articles on the same trend. The day's snapshots, hour by
  hour, are listed below.
- GitHub Trending: GitHub sources read the Trending page (repositories
  developers are starring today, with stars gained today) instead of
  word searches, which found new repositories nobody used. A newsroom's
  automatic GitHub searches are suspended and replaced by GitHub Trending
  for the same site; a status a person chose stays as it is. A trending
  repository's summary says its rank and the stars gained, and its stars
  of the day count towards the topic's Hype.

### Changed

- Drafts are richer: besides the proposal's citations, the model gets
  other recent news on the same subject as context, and a target length
  taken from the articles of the site.
- Proposals use the site's own rating and name of a source, not the
  archive's.
- The daily items limit of a plan counts the new items of the sources a
  newsroom follows; it no longer stops the shared collection.

## 0.9.3

### Fixed

- Core `/v1/fetch` took a "304 Not Modified" for a redirect and failed
  with "Redirect without Location". Every feed answering a conditional
  GET (ETag, Last-Modified) failed from its second read on, so Editor
  Mode suspended most sources after a few hours and collected almost
  nothing. Only 301, 302, 303, 307 and 308 are followed now; sources
  suspended by this error go back to work at the next collection.
- Editor Mode: a fetch the Core could not make (the newsroom used up its
  daily fetches, or the Core was down) no longer counts as a failure of
  the source. Before, a night past the limit suspended every source for
  "failed fetches in a row". Sources suspended that way go back to work
  at the next collection; a status set by a person stays as it is.

### Added

- The home page says when the last round could not read sources because
  the daily fetches ran out or the Core did not answer; the Fonti page
  shows each source's last read error.

### Changed

- `deploy/server/core.example.yaml`: the default daily fetches of a
  newsroom go from 2000 to 10000. Hourly collection of 60 sources is
  already about 1500 a day.

## 0.9.2

### Changed

- Editor Mode: every proposal, on the home page and in "Trend e piano",
  says in plain words what it is: the proposed article and its format,
  the news it comes from (link, source and date), why it suits the site
  (the editorial profile subject it falls under, and why now when the
  model wrote it) and what its two scores mean. A low Hype from a single
  source is explained, not left as a bare "Hype 0".
- A proposal made without the model takes the newest headline as its
  title instead of the topic's keywords.

## 0.9.1

### Added

- Editor Mode: every page opens with "Come funziona questa pagina": what
  it does, what to expect and what to check, in the five languages; it
  can be closed and the browser remembers it. A new "Guida" page lists
  the five decisions left to people and every step of the newsroom, from
  the domain to the published article.

### Changed

- The language selector is at the top right of every page.

## 0.9.0

### Added

- The Editor Mode dashboard, the account page and the console in five
  languages: English, Italian, German, French and Spanish. The language
  comes from the selector at the bottom of every page (kept a year in a
  cookie), else from the browser, else `EDITOR_LANGUAGE` (English by
  default). Notices, errors, the invite and password emails follow it;
  the trial reminder follows the newsroom's language.
- Signup from the public site: `"language"` in the body (or
  `Accept-Language`) sets the language of the error messages, of the
  owner's invite and of the newsroom.
- Signup from the public site: the plan the visitor picked is kept as
  `desired_plan`/`desired_interval` while the newsroom is on trial, and is
  already selected in `/account`. A paid plan never starts without its
  payment from the site's key (it did before when `plan` was sent).
  A payment recorded by bank transfer clears the wish too.

### Fixed

- Local installation (SQLite): the queue no longer runs the work of a
  suspended newsroom.
- Core: the delegate limits of a token whose tenant is itself nested
  (`parent/child`) apply to its sub-tenants.
- Account page: billing data for a newsroom that does not exist give a
  message instead of an error.
- A payment recorded outside the gateway (bank transfer) cancels the
  automatic renewals at Mollie, so the customer is not charged twice. If
  Mollie does not confirm the cancellation, or online payment is not
  configured, nothing is recorded and the console says why, instead of
  leaving the renewals running.

### Upgrading from 0.8.3

- Migration 0016 adds two columns to `editor.subscriptions`; it runs by
  itself when the containers start.
## 0.8.3

### Changed

- A proposal can also come from one strong news item: a topic with an
  opportunity of at least 50/100 from a source scored 0.60 or more. Two
  items from two different sources still make a story at any score.
- The day's proposals no longer stop at the first topics that fail the
  rules: the next ones are tried until the day's number is reached.

### Added

- "Proponi adesso" in Trend e piano: the day's proposals now, without
  waiting for the briefing, up to the number a day set in the profile.
- Sources page: how many news items each source brought in the last 7 days.
## 0.8.2

### Fixed

- A proposal needs at least two news items from two different sources: the
  same repository found by two GitHub searches was enough before, and gave
  topics like a namesake repository.
- Editor home page, "Opportunità del giorno": only topics that could become
  a proposal (two items from two sources), each with how many items and
  sources it has.
- A source the daily maintenance suspended is no longer counted in "Da
  decidere": it is listed under Raccolta, saying nothing has to be done.
- "Primi passi": after discarding proposals the step says so and when the
  next ones come; the proposal button reads "Accetta e scrivi la bozza",
  because accepting already writes the draft.
## 0.8.1

### Fixed

- Editor home page: "Apri il registro" under a suspended source opened the
  sources of the site in view, which could be another site, so the list
  was empty. It now switches to the source's own site first, and the
  suspended source shows which site it belongs to.
- Sources page: when the site in view has no source in the chosen state,
  it names the other sites that do, with a link to each.
- "Primi passi" counts the sources and news of the site in view, like the
  Fonti page; the "fonti attive" count under Raccolta says when it covers
  every site.
## 0.8.0

### Added

- Selling the service from the backoffice, ready for a public site and
  for resellers:
  - **Plans** in the console (`/admin` → Piani): prices without VAT,
    monthly and yearly, trial days, limits (sites, languages per site,
    active sources, items a day, articles a month). The provisional
    catalogue is Prova (7 days), Starter 49 €, Pro 149 €, Business 399 €.
    The public site reads it from `GET /api/public/plans`, without a key.
  - **Customers**: the billing data of whoever pays (company or person,
    VAT number, tax code, address, SDI code, PEC, checked), sent by the
    site with `POST /api/operator/signup` together with the newsroom on
    trial and its owner's invite; `GET`/`PATCH /api/operator/customers`.
  - **Trial and its end**: an email to the owner two days before; when the
    trial (or a paid period, after three days of grace) ends, the newsroom
    stops but keeps its data, and its owner signs in to `/account` to
    choose a plan. A banner in the newsroom counts the days left.
  - **Payments**: with `EDITOR_MOLLIE_API_KEY` the owner pays from
    `/account` (card, PayPal and the other Mollie methods) and the
    renewals are automatic; Mollie's notices arrive at
    `/api/billing/mollie/webhook`. Without it, bank transfers are recorded
    from the console or with `PUT /api/operator/tenants/{id}/subscription`.
    The gateway sits behind a small interface, so another can replace it.
  - **Resellers** (`/admin` → Rivenditori): an API key bound to the
    reseller signs up and manages only its own customers, which it bills
    itself.
  - The console lists customers with their billing data and newsrooms, and
    every newsroom shows its plan, state and paid period.
- "Primi passi" on the home page: the way from the domain to the first
  published article, step by step, with what the newsroom is waiting for
  (the scan, the sources, the first collection, the morning proposals) and
  what is up to you. It goes away once every step is done.

### Upgrading from 0.7.7

The database migration runs by itself. Newsrooms that already exist have
no subscription and keep working as before; give one a plan from the
console when it should be billed. For online payments add
`EDITOR_MOLLIE_API_KEY` to `.env` (and `EDITOR_PUBLIC_URL`, which Mollie
needs to reach the server), and to a `compose.yaml` written by an earlier
`server-init` the line `EDITOR_MOLLIE_API_KEY: ${EDITOR_MOLLIE_API_KEY:-}`
under `x-editor-env`.

## 0.7.7

### Added

- A spare model for the main one: `fallback:` at the top of the
  configuration names a model of `models` the Core uses when the provider
  fails (an error, a rate limit, a timeout); a spare can have its own. The
  server's `core.example.yaml` has a commented example with Mistral and
  OpenRouter's free models, and `compose.yaml` passes their keys
  (`LLM_FALLBACK_API_KEY`, `LLM_FALLBACK2_API_KEY`).
- `/v1/llm/chat` answers with `served_model`, the provider's model that
  answered. The dashboard shows it bottom left, instead of "default".

### Fixed

- A newer re-analysis replaces the proposal of an older one still waiting:
  two versions to approve (v2 and v3) no longer pile up.
- A topic taken out of a proposal stays out after the next analysis too.

### Changed

- "Aggiungi un dominio" moved from the bottom of the profile page to
  Impostazioni ("Siti della redazione"), with a link under the site picker.
  The profile page keeps it only when there is no site yet.
- When a proposal is compared with the approved profile, the topics that
  leave are listed in the comparison only, not again in the topics table.

### Upgrading from 0.7.6

- To use a spare model on an existing server, add `LLM_FALLBACK_API_KEY`
  (and `LLM_FALLBACK2_API_KEY`) under `core:` → `environment:` in your
  `compose.yaml`, as in `deploy/server/compose.yaml`, and the `fallback:`
  block to `core.yaml`. Nothing to do otherwise.

## 0.7.6

### Fixed

- A proposal no longer rests on a single item: a topic needs at least two
  before the plan proposes an article on it. A new repository with no
  stars, found because it shares a name with the site's product, had
  become a proposal on its own.
- GitHub searches found by source discovery keep only repositories with at
  least 10 stars; the ones already found get the same filter at the next
  source maintenance. A repository's title now carries its description
  ("owner/name: what it is"), so topics made of repositories have a
  readable name.

### Changed

- The editorial plan says what accepting means (the idea goes in the plan,
  then "Scrivi la bozza" writes the article), what Opportunity and Hype
  measure, both out of 100, and the format in Italian.
- Release: the server's Core image is made from the Linux x64 binary the
  release has just built, instead of compiling everything again inside
  Docker. The release build starts from main's compiled dependencies (a new
  CI job keeps them cached on main). A pull request from a branch of this
  repository no longer runs CI twice: its push already did.

## 0.7.5

### Added

- Topics can be taken out of a site's line ("Togli il tema"): they are no
  longer followed and later analyses do not propose them again. "Rimetti"
  brings one back. On a draft the change is made in place; on an approved
  profile it becomes a new approved version.
- Profile versions that are not in use can be deleted from the Versions
  table. The approved one cannot, nor one that already produced proposals
  and scores.

### Fixed

- A site without an RSS or Atom feed, or without a sitemap, is no longer
  called partial for that alone: the articles are found anyway. These are
  shown as notes.
- The reasons of an incomplete analysis, and why a source was suspended,
  are shown in Italian.
- Hacker News searches found by source discovery followed the most popular
  stories of all time, so they looked silent and were suspended after 45
  days. They now follow the newest stories; the ones already suspended for
  this are moved and reactivated by the next source maintenance.

### Changed

- A proposed profile says what changes against the approved one, row by
  row: sector, audience, tone, languages, each topic (new, leaving, share
  before and after), and why topics found on every page leave.
- Home: a Sites card lists every domain with the version in use, and the
  one to approve highlighted only when there is one. Profiles to approve
  are listed by domain and version.
- The sidebar shows the Editor Mode version and the model last used, in
  small print. Editor Mode's own version now follows the release
  (`editor-mode/pyproject.toml`, was 0.1.0).
- Calmer colours: a suspended source is shown in teal; red is kept for
  errors and blocking problems.

## 0.7.4

### Fixed

- Analysing a site again now proposes an updated profile when the analysis
  method has improved or the new reading finds what the old one missed
  (the sector), even if a profile is already approved. Before, an approved
  profile stayed as it was until the site itself changed.
- Source discovery keeps only the feeds published on the source's own
  domain (euractiv.com no longer brought in another site's feed).

### Changed

- The profile page explains the editorial line in plain words: languages,
  pace, length, topics with their keywords and two example articles each,
  and what approving means step by step. When the analysis is incomplete
  the main button is "Rianalizza", and approving stays possible.
- The sources page says how each source was found (cited by the site's
  articles, found by searching a term on Hacker News, GitHub, arXiv or
  Hugging Face, or suggested by the model and verified), shows its latest
  news, the reason a source was rejected and how its score is made up.
- More sources per site: one search per topic as well as the model's
  terms (up to 8), and up to 15 domains suggested by the model, also in
  the site's languages.

## 0.7.3

### Fixed

- A domain that redirects to another host (rag-pro.eu to rag.i3k.eu) is
  read where it lands: before, its sitemap, links and articles were all
  discarded as another site's, and the analysis found nothing.
- A site analysed again before its profile was approved showed two
  profiles to approve on the home page: the newer analysis now replaces
  the draft still waiting.
- Work in progress names the site by its domain ("Ricerca fonti
  (i3k.eu)"), not by its number.

### Changed

- The profile page says what you approve: a summary of the editorial line
  written by the model from the pages read, and what the agent will do
  with it (which sources it looks for, which news it keeps, how trends are
  scored, how many proposals a day, in which language and tone the drafts
  are written). An incomplete analysis says so before you approve it.

## 0.7.2

### Fixed

- Site analysis: keywords found on every page (a site-wide meta keywords,
  or a tag put on everything) are no longer taken for subtopics, which all
  showed 100%. They are kept as the site's keywords and given to the model.
  Each article now counts for one subtopic only, in the analysis without
  the model too.
- The analysis reads up to 25 articles (was 10, fewer than the 15 it asks
  for to call itself complete) within 60 pages (was 40).
- When the model's part fails, the profile says why.

### Added

- Languages: each article's language is read, the profile shows the
  articles per language ("Lingue: it (12), en (11)") and how many are
  translations of each other (hreflang), and the main language is the one
  most articles are in.

## 0.7.1

### Changed

- **The administrator is created at installation.** `server-init` asks
  for your email and password (typed twice, not shown, written nowhere)
  and the name of a first newsroom, and once the stack is up creates the
  administrator account and the newsroom, owned by it: you sign in at
  `/login` and land on `/admin`; `/editor` opens the newsroom. Without
  questions: `--admin-email`, `--newsroom` and `EDITOR_ADMIN_PASSWORD`.
- With `docker compose up -d` alone, `EDITOR_ADMIN_EMAIL`,
  `EDITOR_ADMIN_PASSWORD` and optionally `EDITOR_FIRST_NEWSROOM` in `.env`
  do the same at the first start. They are used only while no
  administrator exists; empty, the first-start link of 0.7.0 is printed.
- New `editor admin-init <email> [--newsroom NAME] [--password-stdin]`,
  which `server-init` runs. With an administrator already there it
  changes nothing.

## 0.7.0

### Changed

- **First start from the browser.** `docker compose up -d` starts
  everything; while there is no administrator, the start prints a
  single-use link (`docker compose logs editor-migrate | grep setup`, or
  `editor setup-link` for a new one) where you choose the email and
  password of the administrator. The login page says how to find it.
- **`/admin` and `/editor`.** The administration (formerly `/operator`,
  which now redirects) is at `/admin`: an administrator lands there after
  signing in. `/editor` opens the newsroom (an administrator's first one;
  "Entra" in `/admin` opens any other).
- `server-init` asks only for the model and its key: it no longer creates
  a newsroom or an account, it prints the first-start link. The options
  `--tenant` and `--email` are gone; newsrooms are created from `/admin`.

### Upgrading from 0.6.x

Nothing to do if you already have an administrator account: sign in and
you land on `/admin`. Without one, after updating run
`docker compose exec editor-web editor setup-link` and open the link.

## 0.6.1

### Fixed

- `editor user-add --tenant` with an id that does not exist stops with
  "no newsroom …" and the list of the newsrooms, instead of a database
  error after creating the account halfway (its password never shown).
  Account, password and membership are now written together.

## 0.6.0

### Added

- Editor Mode: **accounts**. People sign in with email and password
  (stored as scrypt hashes; five wrong passwords lock the account for 15
  minutes) and may belong to several newsrooms, each with its role. An
  owner invites people from Impostazioni (lettura, editor, owner), changes
  their role or removes them: a removed person is out at once. Invites and
  password resets are single-use links, sent by email when SMTP is set and
  shown otherwise. Tokens remain for integrations.
- Editor Mode: **operator console** (`/operator`, for operator accounts):
  every newsroom with plan, limits and usage; create one and invite its
  owner, change plan and limits, suspend and reactivate, delete; enter a
  newsroom as owner; invite other operators; password-reset links; API
  keys for the billing system.
- Editor Mode: **provisioning API** (`/api/operator/tenants`, with an
  operator key `edo_...`): create a newsroom when a payment goes through
  (its owner gets the invite), read them with their usage, change plan and
  limits, suspend (payment failed), invite, delete. An `external_ref` (the
  subscription) makes a repeated notice return the same newsroom.
- `editor user-add` (account, optionally operator and member of a
  newsroom; prints a generated password) and `editor operator-key`.
- `server-init` asks for your email and creates your account, operator of
  the service and owner of the first newsroom, instead of a login token.
  It asks the newsroom's name (the id is derived from it) and does not
  show the model's key while it is typed.
- Editor Mode: the EuLLM logo in the dashboard.

### Fixed

- Editor Mode on a server: `editor tenant-delete` deleted nothing and
  failed, because row-level security applies to the schema owner too
  (it is not a superuser there). It now deletes in the tenant's scope.
- Editor Mode: the site page said "Nessun sito" while the first analysis
  was still queued or running, or had failed: it now shows the work and
  its error before the site exists.

### Upgrading from 0.5.4

In the installation folder (`~/eullm-server`):

    sed -i 's/^EULLM_VERSION=.*/EULLM_VERSION=0.6.0/' .env
    docker compose pull && docker compose up -d

The migration adds the account tables. Then create your account, operator
of the service and owner of your newsroom (the password is printed once);
the first command shows the newsroom's id:

    docker compose exec -T postgres psql -U postgres -d eullm -Atc "SELECT tenant_id, name FROM editor.tenants"
    docker compose exec editor-web editor user-add you@example.com --operator --tenant <id> --role owner

and sign in with email and password. Existing tokens keep working. For
invite links that point to the public address of the dashboard, add
`EDITOR_PUBLIC_URL: ${EDITOR_PUBLIC_URL:-}` under `x-editor-env` in
`compose.yaml` (server-init does not rewrite an existing file) and set
`EDITOR_PUBLIC_URL` in `.env`; set SMTP to send them by email.

## 0.5.4

### Added

- `eullm-agent server-init`: the server installation set up and started in
  one command, with no checkout of the sources. It writes what is missing
  in its folder (compose file, `core.yaml` for the model chosen, `.env`
  with every password, token and key generated and readable only by its
  owner), starts the stack with Docker and creates the first newsroom,
  printing its owner's login once. Run again, it changes no existing file.
- Every release publishes the images of the Core
  (`ghcr.io/eullm/eullm-agent`) and of Editor Mode
  (`ghcr.io/eullm/eullm-agent-editor`), Linux x86-64. `deploy/server`
  pulls them (`EULLM_VERSION` in `.env`, `latest` when empty) instead of
  compiling; `docker compose up -d --build` still builds from the checkout.

### Fixed

- Editor Mode: subtopic shares count each article once, for the subtopic
  it matches best, and words found in every article (the site's own name)
  weigh nothing: shares no longer read 100% for several subtopics.
- Editor Mode: approving a profile retires the older drafts still
  waiting, which no longer ask for a decision.

### Upgrading from 0.5.3

`git pull`, then in `.env` add `EULLM_VERSION=0.5.4` and run
`docker compose pull && docker compose up -d`. The first time, the two
images must be made public on GitHub (organisation eullm → Packages →
each image → Package settings → Change visibility); until then
`docker compose up -d --build` keeps building them on the server.

## 0.5.3

### Fixed

- **Core on a server: it stopped starting after the first restart.** Its
  database role is named `core` like its schema, so once the schema
  existed the migration history was looked for there, not found, and every
  migration applied again ("relation runs already exists"). The history is
  now always read from schema `public`. A server already stuck needs, once:
  `docker compose exec postgres psql -U postgres eullm -c "ALTER ROLE core
  SET search_path = public"`, or this version.
- Editor Mode: when robots.txt cannot be read, the site page now shows why
  (for example the Core's answer), not only the error type.

The v0.5.3 tag still reports version 0.5.2.

## 0.5.2

### Added

- `provider.max_tokens` for `type: openai`: the output tokens per answer
  sent to the service. Reasoning models (for example on NVIDIA Build)
  count their reasoning in it, and with the service default the answer
  can come back empty. Without it nothing is sent, as before.

## 0.5.1

### Fixed

- **Editor Mode on a server: queued work never started.** The queue
  functions run as the schema owner, and row level security on
  `editor.jobs` applied to the owner too: where the owner is not a
  superuser, as in `deploy/server`, the worker found no work and site
  analyses stayed "in coda". Migration 0013 lifts it for the owner only;
  the application role is still filtered. The local installation
  (SQLite) was not affected.
- The worker logs every job it starts and finishes, with its duration.

### Upgrading from 0.5.0

`git pull && docker compose up -d --build`: the migration runs at start
and the work already queued starts by itself.

## 0.5.0

Editor Mode ready for many newsrooms on one server: durable queued work
served in turn, every newsroom a Core tenant of its own with its own
budget, publishing credentials entered from the dashboard and stored
encrypted, and a newsroom's data exported or deleted whole.

### Added

- **Sub-tenants for applications with many customers.** A Core token with
  `delegate: true` may add `X-EuLLM-Tenant: <name>` to any request and act
  as tenant `<tenant>/<name>`: its own runs, approvals, usage, audit and
  limits. `PUT /v1/tenants/{name}/limits` sets a sub-tenant's limits,
  stored in the database (migration 0004, SQLite 0002) and effective at
  once, with no change to the configuration and no restart;
  `delegate_limits` applies to a sub-tenant that has none. Limits in
  `api.tenants` still take precedence. Any other token is refused with the
  header, and a sub-tenant cannot set limits.
- Editor Mode uses it with `EDITOR_CORE_DELEGATE=true`: each tenant's model
  calls and fetches run as its own Core tenant, and `editor tenant-plan
  --max-cost-month` also sets that tenant's monthly budget in the Core,
  where calls under way are reserved. `deploy/server` is configured this
  way. A tenant with its own token (`--core-token-env`) keeps using it.

- **Editor Mode: publishing credentials from the dashboard.** The owner
  of a tenant enters the WordPress application password, the webhook key
  or the Telegram bot token when adding a target (page Pubblicazioni, or
  `PUT /api/secrets/{name}`); it is stored encrypted with AES-256-GCM in
  `editor.secrets`, bound to its tenant and name, and never shown again,
  only replaced or deleted. The key is `EDITOR_SECRET_KEY` (`editor
  secret-key` prints one); the local installation creates its own key
  file next to the database. A credential in the database comes before the
  `EDITOR_SECRET_<TENANT>__<NAME>` variable, which keeps working.
- The dashboard asks for the WordPress user of a target, which it did not
  before, so a WordPress target added from the dashboard could not
  publish.

- **Editor Mode: a tenant's data, whole.** `editor tenant-export` writes
  every row of a tenant as JSON, credentials left out; the owner downloads
  the same file from Impostazioni (`GET /api/export`). `editor
  tenant-delete <tenant> --confirm <tenant>` deletes a tenant and all its
  data in one transaction. The daily maintenance deletes collected items,
  their observations and finished jobs older than `EDITOR_RETENTION_DAYS`
  (default 365, `0` keeps everything); items cited by a proposal or a
  draft and model usage are kept.

### Changed

- **Editor Mode: durable job queue, fair between tenants.** Site analyses,
  source discoveries, drafts and publications asked from the dashboard or
  the API are written to `editor.jobs` as `queued` and run by `editor
  worker`, no longer inside the dashboard process: a restart of either
  loses nothing. Every hour the worker queues one tick per tenant instead
  of running all tenants one after the other. A tenant runs one job at a
  time, the tenant served least recently goes first and, within a tenant,
  work a person asked for comes before the hourly tick, so one tenant's
  queue cannot hold up the others. A job whose worker dies (no heartbeat
  for five minutes) is queued again, up to three attempts; an error raised
  by the work itself is recorded and not retried, so a failed draft is not
  paid for twice. `editor worker --concurrency N` (or
  `EDITOR_WORKER_CONCURRENCY`, default 2) runs up to N tenants at the same
  time on PostgreSQL; on SQLite it runs one job at a time. On SIGTERM the
  worker stops claiming and waits for the running jobs.
- The queue is the same on PostgreSQL and SQLite: Editor Mode no longer
  depends on procrastinate. `editor tick` queues every tenant's tick and
  runs the waiting work in the foreground.

### Upgrading from 0.4.0

The Core applies its migration 0004 by itself at start. For a server
installed with `deploy/server`: add `delegate: true` and a
`delegate_limits` line to the `editor-mode` token of `core.yaml` (see
`core.example.yaml`; `compose.yaml` already sets `EDITOR_CORE_DELEGATE`),
then run `editor tenant-plan <tenant> --max-cost-month <budget>` for each
tenant to give it its limit in the Core. Usage counted before under the
shared `editor` tenant stays there; each tenant starts counting as
`editor/<tenant>`.

Run `editor migrate` (PostgreSQL revisions 0011 and 0012, SQLite files
`0002_job_queue` and `0003_secrets`), then restart the dashboard and the worker. The
`procrastinate_*` tables left by earlier versions are no longer used and
can be dropped. The dashboard now only queues work: keep `editor worker`
running next to it (the local installation and `deploy/server` already
do).

For credentials from the dashboard on a server, add `EDITOR_SECRET_KEY`
to `.env` (`openssl rand -base64 32`) and keep a copy with the backups.
Without it everything works as before, with credentials in the
environment.

## 0.4.0

Two installations, both with a web interface. On your computer: one
command, data in SQLite, nothing else to install. On a server for several
tenants: PostgreSQL, the Core and Editor Mode with `docker compose`.

### Added

- **Local installation** (`install/local.sh`, Linux and macOS): one command
  puts the Core and Editor Mode in `~/.eullm-agent`, with the Python Editor
  Mode needs brought by uv, and adds the `eullm-local` command. From a
  checkout: `sh install/local.sh --source .`.
- **`eullm-agent start`**: the agent on one computer, no setup by hand. It
  opens the browser with a link that logs in by itself (a new token at
  every start, never written to disk), keeps its state in SQLite next to
  the config, uses an EuLLM Engine it finds at `localhost:11434` the first
  time (or asks for a provider) and listens on this computer only. When
  Editor Mode is installed next to the config, it also runs its dashboard
  (`127.0.0.1:8090`) and hourly worker, and prints the dashboard's login
  token; logs go to `logs/`.
- **Web interface** of the Core, served by `start` and `api` at `/`: chat
  (each message is a task), tasks with every model and tool call,
  approvals, usage of today and this month. It reads everything through
  `/v1` with the browser's token, under a strict content security policy.
- **SQLite store** for the Core (`database.url: sqlite://eullm-agent.db`):
  the same tables, append-only audit and startup recovery as PostgreSQL,
  checked by the same tests.
- **Editor Mode on SQLite** (`EDITOR_DATABASE_URL=sqlite:///editor.db`): one
  file with one tenant (a second is refused), the same code as PostgreSQL,
  and `editor worker` as a plain hourly loop. Every database test runs on
  both.
- **Server installation** (`deploy/server`): `docker compose` with
  PostgreSQL 18, the Core, Editor Mode's dashboard and worker, database
  roles created at the first start, and a guide (HTTPS, backups, updates).
  Images: `Dockerfile` (Core) and `editor-mode/Dockerfile`.
- `GET /v1/profiles`: the profiles a token may use.

### Changed

- `editor migrate` on PostgreSQL also creates the worker's job queue;
  `editor worker --apply-schema` is no longer needed.
- Editor Mode counts tenant usage in its own code instead of the
  `editor.tenant_usage` view, with days and months in the tenant's time
  zone as before.
- `Cargo.lock` is in the repository and CI and release builds use
  `--locked`, so every build of a tag uses the same dependency versions.
- Dependabot proposes dependency updates (Rust, Editor Mode, GitHub
  Actions) once a week as pull requests; minor and patch updates are
  grouped, a major version comes on its own. First round merged:
  `serde_json`, `actions/checkout` 7, `actions/upload-artifact` 7,
  `actions/download-artifact` 8, `softprops/action-gh-release` 3.
- Editor Mode: `pyyaml`, never imported, is no longer a dependency.

### Upgrading from 0.3.x

Nothing to change: PostgreSQL installations keep working as they are and
the Core applies no new migration. `editor migrate` needs no new revision
either; run it once to create the job queue if the worker used
`--apply-schema`.

## 0.3.6

### Security

- `run_program` and module tools run in a bubblewrap sandbox by default
  (`tools.sandbox`): no network, system directories read-only, only the
  workspace visible and read-only, own PID namespace (nothing a program
  starts outlives it), memory, file size and core limits. If the sandbox
  cannot work the service does not start; `tools.sandbox.mode: none` turns
  it off knowingly.
- A tainted run that has also read local files or documents needs approval
  for `fetch_url` (`taint.private_sources`, `taint.egress`), so injected
  instructions cannot send private data out.
- Editor Mode: a tenant names only its publishing secret; the server reads
  `EDITOR_SECRET_<TENANT>__<NAME>`, never another variable. Tenant ids are
  lowercase words joined by single dashes.
- Editor Mode: gzip sitemaps are inflated within the size limit (no gzip
  bomb), and a truncated one no longer stops the analysis.
- Editor Mode: robots.txt is checked under the name the Core sends
  (`eullm-agent`), also before reading the articles cited in drafts.
- Editor Mode: a deactivated tenant loses access to API and dashboard at
  once (migration 0010), not only its scheduled work.
- Editor Mode: publications connect to the address that was checked, so a
  name that changes between lookups (DNS rebinding) cannot reach internal
  addresses; environment proxies are not used for publications.
- Editor Mode: SMTP STARTTLS verifies the server certificate before the
  login sends the password.
- Monthly budgets count model calls under way at the month's average per
  call, so concurrent requests cannot all slip under the limit.
- A provider that does not report token usage no longer makes calls free:
  the Core estimates it. The `default` model can be priced (`pricing` at
  the top level), and the Core warns at start when a cost budget applies to
  a model without a price.

### Changed

- Editor Mode: source discovery and relevance checks follow the approved
  profile only (approving a version starts discovery), and maintenance
  never reverses a status a person set.

### Reliability

- Approvals: a waiting run also reads its decision from the database, so a
  decision taken through another Core process reaches it, and a decision
  that lands just before the expiry wins. At startup, runs left unfinished
  by the previous process fail as interrupted. The API stops gracefully.
- Tenant limits: concurrent runs and fetches cannot all pass the same
  check; the monthly budget is re-checked before every model call of a run.
- `api.fetch.credentials`: the Core adds a header (e.g. a GitHub token) for
  an exact host, over https and on the first request only.
- Editor Mode: background work is recorded as jobs with its outcome
  (shown on the site page and at `GET /api/jobs`). Each scheduled step
  runs on its own; maintenance and the weekly review run based on their
  last successful run, so a late tick no longer skips them. An undelivered
  briefing is sent again within its window; recipients get it in Bcc.
- Editor Mode: a publication is marked `sending` before anything leaves; one
  cut short waits for a person to check the CMS instead of being sent twice.
- A fallback answer is recorded and billed as the model that served it,
  with that model's price, not the primary's.
- An approval wait ends when its run's time limit does, so a forgotten
  approval no longer holds a run slot past the run's deadline.
- Two Core processes on the same database (a rolling restart, a second
  instance) no longer close each other's runs at startup: each process
  holds an instance lock and recovery skips runs of a live one
  (migration 0003, applied at start).
- Editor Mode: a profile edit (`PUT /api/sites/{id}/profile` and the
  dashboard) is validated and refused with 400 if malformed, instead of
  breaking the site pages.
- Editor Mode: quota checks and profile and draft version numbers are safe
  under concurrent requests.
- YAML is read and written with `serde-saphyr`; `serde_yaml` is deprecated.

## 0.3.5

### Added

- Editor Mode (`editor-mode/`, Python): the first vertical built on the
  Core, now part of this repository under AGPL-3.0-or-later. Domain-first
  editorial work: site analysis and an editorial profile with evidence,
  source discovery and rating, Hype and Opportunity scores, an editorial
  plan, drafts where every sentence cites a stored source, publication to
  WordPress, webhooks and Telegram after an owner's approval, and a
  seven-page dashboard. Plan limits per tenant on top of the Core's.

### Changed

- README: Editor Mode shown with its dashboard.
- Pull request template: no CLA section; the CLA check tells external
  contributors what to add.

## 0.3.0

The Core as a service: other programs can start runs, make model calls,
decide approvals and fetch web pages over HTTP, with state and audit in
PostgreSQL and limits per tenant. Applications such as Editor Mode are built
on this API instead of calling providers or the web directly.

### Added

- `eullm-agent api`: HTTP API (`/v1/runs`, `/v1/llm/chat`, `/v1/approvals`,
  `/v1/health`) with bearer tokens per tenant, documented in
  `docs/openapi.json` and checked by contract tests.
- `eullm-agent token new` to create API tokens (only the SHA-256 is stored).
- PostgreSQL store (`database`): schema `core` with runs, model calls, tool
  calls, approvals and an append-only `audit_events` table; migrations run
  at start. Without a database the state is kept in memory.
- Model Router (`models`): named models with fallback chains and pricing.
- Profiles (`profiles`): model, allowed tools, iteration and time limits,
  token and cost budgets per run.
- Policy engine (`policy_file`): allow / deny / require_approval per tool and
  profile; runs that read external content are tainted and their side
  effects need approval.
- Approval queue: runs wait for a decision from the API, from Telegram
  (`/approve`, `/deny`) or from the terminal; no answer in time is a refusal.
- Telegram tasks run in the background so approvals can be answered, and use
  `telegram.profile`.
- `POST /v1/fetch` (`api.fetch`): HTTP GET for applications built on the Core
  (crawling, feeds) with the same address checks as `fetch_url`, every
  redirect re-checked, a size limit and a per-host pace shared by all
  callers; each request is recorded in `core.fetches` and the audit.
- Limits per tenant (`api.tenants`): monthly cost and tokens, runs and
  fetches per day, in UTC calendar periods; a request over a limit gets 429
  with the reason. `GET /v1/usage` reports the tenant's usage and limits.

### Changed

- The audit log records the policy decision of every tool call and the
  token totals, cost and taint of every run.

## 0.2.0

Security hardening release (Sprint F0). Configurations from 0.1.x still load,
but anything risky they enabled is now off until turned on explicitly.

### Breaking changes

- `provider.type: eullm` now uses the EuLLM Engine's OpenAI-compatible
  `{base_url}/v1/chat/completions`, where tool calling works. For a plain
  Ollama server use the new `provider.type: ollama`.
- The `shell` tool is replaced by `run_program` (`tools.exec`): disabled by
  default, runs only programs in `allowed_programs`, never through a shell.
  An old `tools.shell` section enables nothing.
- File tools are confined to `workspace`; `tools.filesystem.allowed_paths` is
  ignored (a warning is printed). `write_file` needs `allow_write: true`.
- `fetch_url` is disabled by default; when enabled it allows HTTPS GET to
  public addresses only.
- Modules are disabled by default and installed only by an operator with
  `eullm-agent module install <name>`. The `install_module` tool is removed.
- `eullm-agent serve` refuses to start when `telegram.allowed_users` is empty.

### Security fixes

- Shell command execution with a bypassable `sudo` filter (S1).
- Path traversal and symlink escapes in `write_file` and `read_file`, and
  unrestricted `list_dir` (S2, S3).
- SSRF in `fetch_url`, including through redirects and DNS rebinding (S4).
- Command injection through module tool arguments, and module installation
  (with `sudo`) triggered by the model (S5).
- Telegram bot open to anyone by default (S6).
- Timed-out commands left running; unbounded tool output (S10).

### Added

- Per-request provider timeouts and one retry on 429/502/503/504 with
  `Retry-After`; error messages carry the provider's status and body.
- Run limits: `limits.max_run_seconds`, `limits.max_tool_output_bytes`.
- JSONL audit log (`audit_log`) of runs, model calls and tool calls, without
  argument values or outputs.
- Secrets from environment variables: `api_key_env`, `token_env`.
- `max_tokens` for Anthropic; token usage parsed for every provider.
- Anthropic responses with unknown block types (e.g. thinking) are accepted.
- Malformed tool-call arguments are reported to the model instead of running
  the tool with defaults.
- `eullm-agent module list|install` subcommands.
- Security, provider and agent regression tests (`tests/`).
