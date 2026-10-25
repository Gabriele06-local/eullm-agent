<div align="center">

# EuLLM Agent

**A complete AI newsroom at your disposal, on your own server.**

Self-hosted AI agent platform written in Rust: give it a domain and Editor
Mode reads the site, learns its editorial line, finds its own sources,
spots the trends worth covering and drafts articles where every sentence
cites a source. Nothing is published without a person's yes.

[![CI](https://github.com/eullm/eullm-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/eullm/eullm-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/eullm/eullm-agent)](https://github.com/eullm/eullm-agent/releases)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)](LICENSE)

[Install](#install) · [Editor Mode](editor-mode/) · [Server](deploy/server/README.md) · [Changelog](CHANGELOG.md)

</div>

![Editor Mode dashboard built on the EuLLM Agent Core](editor-mode/docs/screenshots/hero.png)

## A newsroom in a box

[Editor Mode](editor-mode/) does the work of an editorial team for any
site, and keeps the editor in charge:

- **Learns the site.** From a domain alone it reads the pages, sitemap and
  feeds and proposes an editorial profile: sector, audience, style,
  subtopics, each with the pages it is based on. What it cannot
  determine, it says so.
- **Finds its own sources.** No fixed list: it starts from the
  publications the site cites and from searches built on its subtopics,
  rates each candidate on what it actually publishes and lets you activate
  or reject it.
- **Hype is not opportunity.** Trends get two separate scores, how loud
  they are and how much they fit your site, and a daily briefing with the
  topics worth covering.
- **Drafts with citations.** Every claim in a draft points to a stored
  source, and numbers are checked against them.
- **Publishes only with approval.** WordPress, GitHub (a commit for the
  static sites deployed from a repository: Vercel, GitHub Pages), webhooks
  and Telegram,
  after a person says yes; credentials are stored encrypted.
- **Many newsrooms, one server.** Each newsroom is a tenant isolated by
  PostgreSQL row-level security, with its own budget, queue and data
  export.
- **Ready to sell as a service.** People sign in with email and password
  and are invited with a role; an operator console creates newsrooms,
  sets plans and limits and suspends them; a provisioning API lets your
  billing system open a newsroom when a payment goes through.

## Why EuLLM Agent

- **Your data, your models.** Runs on your hardware with a local model
  ([EuLLM Engine](https://github.com/eullm), Ollama) or any
  OpenAI-compatible or Anthropic API. Built in Europe for European
  sovereignty and GDPR-minded deployments.
- **One binary.** The Core is a single Rust executable: `eullm-agent start`
  on a laptop, `eullm-agent server-init` on a server, which generates every
  password and starts PostgreSQL, the Core and Editor Mode with Docker.
- **The model proposes, the policy decides, a person approves.** Every
  sensitive action stops in an approval queue (web interface, API,
  Telegram or terminal).
- **Everything is on record.** Runs, model calls, tools and web fetches
  land in the database with an append-only audit trail.
- **Safe web access.** `POST /v1/fetch` crawls with SSRF checks on every
  redirect, size limits and a per-host pace.
- **Costs under control.** Model router with fallback and pricing, budgets
  per run, monthly and daily limits per tenant, cost reserved before every
  model call.
- **A platform, not a single app.** `eullm-agent api` is a multi-tenant
  Core with an HTTP API; Editor Mode is the first vertical built on it.

## Install

Two installations, both with a web interface:

- **On your computer** (one person, Linux or macOS): one command installs the
  Core and Editor Mode in `~/.eullm-agent`, with Python brought by
  [uv](https://docs.astral.sh/uv/) and the data in SQLite files. Nothing
  else to install.

  ```bash
  curl -LsSf https://raw.githubusercontent.com/eullm/eullm-agent/main/install/local.sh | sh
  eullm-local
  ```

  From a checkout: `sh install/local.sh --source .` (builds with cargo).

- **On a server** (several tenants): PostgreSQL, the Core and Editor Mode
  with `docker compose`, set up and started by `eullm-agent server-init`;
  see [deploy/server](deploy/server/README.md).

## From source

```bash
cargo build --release
./target/release/eullm-agent start     # web interface on this computer
```

`start` opens the browser on `http://127.0.0.1:8088` with a link that logs
in by itself; the link changes at every start and works only while the
program runs. The first time, without a `config.yaml`, it looks for an
EuLLM Engine at `http://localhost:11434` and uses its model, or asks for a
provider. State is kept in `eullm-agent.db` (SQLite) next to the config, so
nothing else needs installing. The interface has a chat (each message is a
task for the agent), the list of tasks with every step, the approvals
waiting for you and the usage of today and this month. `start` listens on
this computer only; for a server use `eullm-agent api`, which serves the
same interface to anyone with an API token. When Editor Mode sits next to
the config (`editor-mode/` with its `.venv`, as the local installation
leaves it), `start` also runs its dashboard on `http://127.0.0.1:8090` and
its hourly worker, and prints the dashboard's login token.

Other ways to use it:

```bash
./target/release/eullm-agent run "List the files in the workspace"
./target/release/eullm-agent serve     # Telegram bot (needs telegram.allowed_users)
```

## Providers

| `provider.type` | Endpoint | Tool calling |
|---|---|---|
| `eullm` | `{base_url}/v1/chat/completions` (EuLLM Engine, OpenAI-compatible) | yes |
| `ollama` | `{base_url}/api/chat` (Ollama native) | yes |
| `openai` | `{base_url}/chat/completions` (default `https://api.openai.com/v1`) | yes |
| `anthropic` | `https://api.anthropic.com/v1/messages` | yes |

Secrets can be given as `api_key` / `token` or, preferably, as `api_key_env` /
`token_env` naming an environment variable. Every provider request has a
timeout (`limits.llm_timeout_seconds`) and is retried once on 429, 502, 503 or
504, honouring `Retry-After`.

**Upgrading from 0.1.x:** `type: eullm` now talks to the Engine's `/v1` API,
because the Engine's `/api/chat` ignores tools. If you were pointing
`type: eullm` at a plain Ollama server, change it to `type: ollama`.

## Security model

EuLLM Agent runs actions chosen by a language model, and a model can be steered
by any text it reads (a web page, a file, a message). So every capability that
touches the machine or the network is **off by default** and narrowly scoped
when turned on. See [config.example.yaml](config.example.yaml) for every
setting.

| Capability | Default | When enabled |
|---|---|---|
| `run_program` (`tools.exec`) | off | Only programs in `allowed_programs`, argv only (never a shell), run in a bubblewrap sandbox (no network, read-only system directories, only the workspace visible and read-only, own process tree, memory and file size limits), killed with everything they started on timeout, output capped |
| `read_file`, `list_dir` | on | Confined to `workspace`: `..`, absolute paths outside it and symlinks leading out are refused |
| `write_file` | off (`allow_write`) | Same confinement; never writes through a symlink; size capped |
| `fetch_url` (`tools.http`) | off | HTTPS GET only; every resolved address must be public (no loopback, LAN, link-local or cloud metadata); each redirect is checked again; the connection is pinned to the checked address; optional domain allowlist; body capped |
| Modules | off | Tools of modules an operator installed with `eullm-agent module install`; arguments are passed as single argv elements, file arguments are confined to the workspace. The agent cannot install modules |
| Telegram | needs config | Refuses to start without `allowed_users`; messages without a sender are ignored |

Each task is also bounded by `max_iterations`, `limits.max_run_seconds` and
`limits.max_tool_output_bytes`. The audit log (`audit_log`, JSONL, mode 0600)
records each run, model call (duration, tokens, errors) and tool call (name,
argument key names, outcome, duration); it never stores argument values,
prompts or outputs.

What this does not protect against: programs you allowlist run with the
agent's own user rights, so allowlisting an interpreter (`sh`, `python`, ...)
gives the model full control of that user. Run the agent as an unprivileged
user, ideally in a container.

## Core service (API)

`eullm-agent api` runs the agent as a service for other programs (for
example Editor Mode). The contract is
[docs/openapi.json](docs/openapi.json).
It also serves the web interface at `/`: open it, paste an API token and
you get the same pages as `start`, limited to that token's tenant.

| Endpoint | What it does |
|---|---|
| `POST /v1/runs` | Start a run with a profile and an input; returns its id at once |
| `GET /v1/runs`, `GET /v1/runs/{id}` | Runs of the caller's tenant, with every model and tool call |
| `POST /v1/llm/chat` | One model call through the Model Router, recorded with tokens and cost |
| `GET /v1/approvals`, `POST /v1/approvals/{id}` | Actions waiting for a person; approve or deny |
| `POST /v1/fetch` | HTTP GET for applications (crawling, feeds) with the address checks of `fetch_url`, a size limit and a per-host pace; off unless `api.fetch` is set |
| `GET /v1/profiles` | Profiles the caller's token may use, with model and tools |
| `GET /v1/usage` | Runs, model calls, tokens, cost and fetches of the caller's tenant today and this month (UTC), with its limits; `api.tenants` sets the limits and a request over one gets 429 |

- **Tokens:** `eullm-agent token new` prints a token and its SHA-256; only the
  hash goes in `api.tokens`. Each token belongs to a tenant and can be limited
  to some profiles. Data is always scoped to the token's tenant.
- **State and audit:** with `database` configured, runs, model calls, tool
  calls, policy decisions and approvals are stored in a database
  (migrations run at start). Two installations:
  - **local, one user:** `url: sqlite://eullm-agent.db`, one file created at
    the first start, nothing to install;
  - **server, several tenants:** a `postgres://` url, schema `core`; several
    Core processes can share it.

  The audit table (`audit_events`) is append-only in both: updates and
  deletes are refused by the database. Without a database the state is kept
  in memory.
- **Model Router and profiles:** `models` adds named models with fallback and
  pricing (`pricing` at the top level prices the `default` model);
  `profiles` choose a model, the tools a run may use and its budget
  (`max_tokens`, `max_cost`, `max_iterations`, `max_run_seconds`). When a
  provider does not report token usage, the Core estimates it (about four
  characters per token) so budgets never count a call as free; cost budgets
  count only models with a price, and the Core warns at start when one would
  never trigger.
- **Policy:** `policy_file` (see [policy.example.yaml](policy.example.yaml))
  decides `allow`, `deny` or `require_approval` per tool and profile. A run
  that has read external content is *tainted*, and from then on tools with
  side effects need approval. A tainted run that has also read local files or
  documents needs approval for `fetch_url` too, so injected instructions
  cannot send private data out. The model proposes, the policy authorises,
  the worker executes and the system records.
- **Approvals:** a run stops at the action and waits for a decision through
  the API, Telegram (`/approve <id>`, `/deny <id> [note]`, sent to the chat
  that started the task) or the terminal for `eullm-agent run`. No decision
  within `api.approval_timeout_seconds` counts as a refusal.

```bash
eullm-agent token new                          # put token_sha256 in api.tokens
DATABASE_URL=postgres://... eullm-agent api
curl -s -X POST localhost:8088/v1/runs -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"input":"List the workspace"}'
```

## Using it with EuLLM Engine

1. Start the Engine and note its address (default `http://localhost:11434`)
   and, if it runs with `EULLM_API_KEYS`, one of the keys.
2. Check that the OpenAI-compatible API answers and that the model is loaded:
   ```bash
   curl -s http://localhost:11434/v1/models -H "Authorization: Bearer $EULLM_API_KEY"
   ```
3. Configure the agent:
   ```yaml
   provider:
     type: eullm
     base_url: http://localhost:11434
     model: qwen3:8b            # a model with tool-calling support
     api_key_env: EULLM_API_KEY # omit if the Engine has no keys
   ```
4. Verify tool calling end to end; the agent must call `list_dir`:
   ```bash
   mkdir -p workspace && echo hello > workspace/note.txt
   RUST_LOG=eullm_agent=debug ./target/release/eullm-agent run \
     "Which files are in the workspace? Read note.txt and tell me its content."
   ```
   The progress lines show `tool:list_dir` and `tool:read_file`, the debug log
   shows `POST http://localhost:11434/v1/chat/completions`, and
   `eullm-agent-audit.jsonl` gets a `tool_call` entry per tool.

## Development

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test                 # unit, provider, agent and security regression tests
cargo test --test security # security regressions only
# PostgreSQL tests run when DATABASE_URL points at a disposable database:
DATABASE_URL=postgres://postgres@localhost/eullm_test cargo test --test pg_store
```

## License

EuLLM Agent is licensed under [AGPL-3.0-or-later](LICENSE). Use it, fork it,
modify it, run it commercially: the one condition is copyleft. If you modify
EuLLM Agent and let others use it over a network (including as a hosted
service), you must offer them the Corresponding Source of your modified
version. Versions published up to and including 0.1.7 remain available to
everyone under their original Apache 2.0 terms; the AGPL governs new work from
this change onwards.

**I3K Technologies Srl holds the copyright** and also offers this software
under a separate commercial licence, for organisations that cannot accept the
AGPL's terms. Enquiries: **info@i3k.eu**

Because the project is licensed both ways, contributions need a Contributor
Licence Agreement: a one-line statement in your pull request. You keep the
copyright in your own work; [CLA.md](CLA.md) explains exactly what it grants
and why it is necessary. See [CONTRIBUTING.md](CONTRIBUTING.md).
