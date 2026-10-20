# Changelog

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
