# Changelog

## Unreleased

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
