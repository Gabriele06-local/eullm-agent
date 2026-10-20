# Server installation

PostgreSQL, the Core and Editor Mode (dashboard and hourly worker) with
`docker compose`, for several tenants isolated by row-level security. For
one person on one computer use the local installation instead
(`install/local.sh`, see the main README).

## What runs

| Service | What it does | Port |
|---|---|---|
| `postgres` | PostgreSQL 18: schema `core` (Core) and `editor` (Editor Mode, row-level security) | none |
| `core` | `eullm-agent api`: model calls, runs, approvals, web fetches, web interface | 8088 |
| `editor-migrate` | `editor migrate` at every start, then exits | none |
| `editor-web` | Editor Mode dashboard and API | 8090 |
| `editor-worker` | queued work (analyses, drafts, publications) and every tenant's hourly tick, a few tenants at a time (`EDITOR_WORKER_CONCURRENCY`, default 2) | none |

The ports listen on 127.0.0.1: put a reverse proxy with HTTPS in front
(below) rather than exposing them.

## Install in one command

Requirements: Docker with the compose plugin, and the `eullm-agent`
binary of the release (Linux x86-64), nothing else:

    mkdir -p ~/eullm-server && cd ~/eullm-server
    curl -LO https://github.com/eullm/eullm-agent/releases/latest/download/eullm-agent-linux-x64
    chmod +x eullm-agent-linux-x64
    ./eullm-agent-linux-x64 server-init --dir .

Everything stays in that one folder: the binary, the compose file,
`core.yaml` and `.env`; the data lives in Docker volumes. It asks only the
model (NVIDIA Build, Groq, EuLLM Engine on this machine or another
OpenAI-compatible service) and its key, then:

- writes the compose file, `core.yaml` and `.env` with every password and
  token generated (`.env` readable only by you);
- downloads the images of the same release and starts everything;
- prints the **first-start link**: open it in the browser and create your
  administrator account (email and password). From `/admin` you then
  create the newsrooms and invite their owners.

Keep a copy of `.env`: `EDITOR_SECRET_KEY` in it encrypts the publishing
credentials. Running `server-init` again changes no file that exists: it
only starts the stack (and prints a new first-start link while there is
no administrator). In a checkout of the sources, `--dir deploy/server`
uses this folder. Without questions: `--provider nvidia --model <name>`
and the key in `LLM_API_KEY`.

## Who sees what

One address, one login; what opens depends on the account:

| Address | Who | What |
|---|---|---|
| `/admin` | administrator (operator): you | every newsroom with plan, limits and usage; create, suspend, delete; invite owners; API keys for the billing system |
| `/editor` (or `/`) | the people of a newsroom: your customers and their staff | the newsroom: site and profile, sources, trends, drafts, publications; the owner also invites the staff in Impostazioni |
| `/login` | everyone | email and password; an administrator lands on `/admin`, everyone else in their newsroom |

Roles inside a newsroom: **owner** (the customer's manager: approves
profiles, sources and publications, manages the people), **editor**
(decides proposals and drafts), **lettura** (reads). An administrator can
enter any newsroom as owner, to help.

## Install by hand

Requirements: Docker with the compose plugin, and this repository.

1. Secrets. Fill in every empty value; `openssl rand -hex 32` makes a good
   password or token.

       cd deploy/server
       cp .env.example .env
       nano .env

2. The model. Pick the provider in `core.yaml` (EuLLM Engine on this
   machine, or an OpenAI-compatible service with its key in `LLM_API_KEY`):

       cp core.example.yaml core.yaml
       nano core.yaml

3. Start. The images of the release come from GitHub (`ghcr.io/eullm`);
   `EULLM_VERSION` in `.env` picks the release, `latest` when empty.

       docker compose pull
       docker compose up -d
       docker compose ps                 # editor-migrate: exited (0), the rest running

4. Your administrator account, from the browser. The first-start link is
   in the log of the start, or printed anew with:

       docker compose logs editor-migrate | grep setup
       docker compose exec editor-web editor setup-link

   Open it, choose email and password, and from `/admin` create the
   newsrooms. (From the command line instead: `editor user-add
   you@example.com --operator`.) The Core's web interface at
   http://127.0.0.1:8088 opens with `CORE_ADMIN_TOKEN` from `.env`.

## Newsrooms, people and the billing system

- **Administration** (`/admin`, for operator accounts): every
  newsroom with its plan, limits and usage; create one (its owner gets an
  invite), change plan and limits, suspend or reactivate, invite people,
  delete; invite other operators, make a password-reset link, and create
  the API keys of the billing system. "Entra" opens a newsroom as owner.
- **People of a newsroom** (Impostazioni, for owners): invite by email
  with a role (lettura, editor, owner), change roles, remove. Each person
  signs in with email and password and can belong to several newsrooms.
  Tokens remain for integrations.
- **Provisioning API**, for the billing system (key from the console or
  `editor operator-key --name billing`, sent as `Authorization: Bearer edo_...`):

  | Call | What |
  |---|---|
  | `POST /api/operator/tenants` | create a newsroom: `name`, `owner_email`, optional `external_ref`, `plan`, `limits`, `timezone`, `language`; 201 with the owner's invite (emailed when SMTP is set, its `url` returned either way). The same `external_ref` again answers 200 with the same newsroom, so a payment notice delivered twice makes one |
  | `GET /api/operator/tenants[?external_ref=...]` | newsrooms with plan, usage and people |
  | `GET /api/operator/tenants/{id}` | one, with its people |
  | `PATCH /api/operator/tenants/{id}` | `plan`, `limits` (null removes a limit), `active: false` to suspend (payment failed), `name`, `timezone`, `language` |
  | `POST /api/operator/tenants/{id}/invites` | invite `email` with a `role` |
  | `DELETE /api/operator/tenants/{id}?confirm={id}` | delete it with all its data |

  Example, when a subscription starts:

      curl -X POST https://editor.example.com/api/operator/tenants \
        -H "Authorization: Bearer edo_..." -H "Content-Type: application/json" \
        -d '{"name": "Redazione Rossi", "owner_email": "rossi@example.com", "external_ref": "sub_123",
             "plan": "pro", "limits": {"max_sites": 3, "max_llm_cost_month": 30}}'

  `limits` takes `max_sites`, `max_active_sources`, `max_items_per_day`,
  `max_drafts_month` and `max_llm_cost_month` (euro; it is also the
  newsroom's budget in the Core).

## HTTPS

A reverse proxy on the same machine, for example Caddy
(`/etc/caddy/Caddyfile`), gets and renews the certificates:

    editor.example.com {
        reverse_proxy 127.0.0.1:8090
    }
    agent.example.com {
        reverse_proxy 127.0.0.1:8088
    }

## Updating

The release to run is `EULLM_VERSION` in `.env`: `server-init` writes the
version of its binary there, so it does not move on its own. When a new
release is out (its job on GitHub Actions has published the images), in
the installation folder:

    cd ~/eullm-server
    sed -i 's/^EULLM_VERSION=.*/EULLM_VERSION=X.Y.Z/' .env    # or empty: always the latest
    docker compose pull && docker compose up -d
    docker compose ps                                          # editor-migrate: exited (0)

Migrations run at start; data stays in the volumes. Read the release's
"Upgrading" notes in [CHANGELOG.md](../../CHANGELOG.md) first: a release may
ask for one more step (a new setting, an account to create). Updating the
binary in the folder is only needed to run `server-init` again:

    curl -L -o eullm-agent-linux-x64 https://github.com/eullm/eullm-agent/releases/download/vX.Y.Z/eullm-agent-linux-x64
    chmod +x eullm-agent-linux-x64

`compose.yaml` is never rewritten by `server-init`. If a release changes it
(its notes say so), take it from `deploy/server/compose.yaml` of that
release, or delete it and run `./eullm-agent-linux-x64 server-init --dir .`
again, which writes only the missing files and changes nothing else.

In a checkout of the sources (`deploy/server`) it is the same, with
`git pull` first.

## Day to day

| What | Command |
|---|---|
| Logs | `docker compose logs -f core editor-worker` |
| Update | see [Updating](#updating) |
| Try a branch | `git checkout <branch> && docker compose up -d --build`: compiles the Core and Editor Mode from the checkout |
| Backup | `docker compose exec postgres pg_dump -U postgres -Fc eullm > eullm-$(date +%F).dump` |
| Restore | `docker compose exec -T postgres pg_restore -U postgres -d eullm --clean < file.dump` |
| Stop | `docker compose down` (data stays in the `pgdata` volume) |
| A tenant's data | `docker compose exec editor-web editor tenant-export acme > acme.json` |
| Delete a tenant | `docker compose exec editor-web editor tenant-delete acme --confirm acme` (for good) |

Each tenant is a Core tenant of its own (`editor/acme`), with its own
usage, audit and limits, through the delegating `editor-mode` token of
`core.yaml`: `tenant-plan --max-cost-month` also sets the tenant's limit in
the Core, and `delegate_limits` covers a tenant whose plan has no budget.
Adding a tenant needs no change to `core.yaml` and no restart.

Publishing credentials (WordPress application password, webhook key,
Telegram bot token) are entered by the tenant's owner in the dashboard,
page Pubblicazioni, and stored encrypted with `EDITOR_SECRET_KEY`. Keep a
copy of that key with the backups: a dump without it holds credentials
nobody can read.
