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
| `editor-worker` | hourly tick: collection, briefing, maintenance, reviews | none |

The ports listen on 127.0.0.1: put a reverse proxy with HTTPS in front
(below) rather than exposing them.

## Install

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

3. Start. The first build compiles the Core and takes a few minutes.

       docker compose up -d --build
       docker compose ps                 # editor-migrate: exited (0), the rest running

4. The first tenant and its owner's login for the dashboard:

       docker compose exec editor-web editor tenant-add acme --name "ACME"
       docker compose exec editor-web editor tenant-plan acme --plan pro --max-sites 4 --max-cost-month 40
       docker compose exec editor-web editor token-new --tenant acme --name francesco

   The last command prints the token (once): it opens the dashboard at
   http://127.0.0.1:8090. The Core's web interface at
   http://127.0.0.1:8088 opens with `CORE_ADMIN_TOKEN` from `.env`.

5. A site: from the dashboard, or

       docker compose exec editor-web editor site-add example.com --tenant acme

## HTTPS

A reverse proxy on the same machine, for example Caddy
(`/etc/caddy/Caddyfile`), gets and renews the certificates:

    editor.example.com {
        reverse_proxy 127.0.0.1:8090
    }
    agent.example.com {
        reverse_proxy 127.0.0.1:8088
    }

## Day to day

| What | Command |
|---|---|
| Logs | `docker compose logs -f core editor-worker` |
| Update | `git pull && docker compose up -d --build` (migrations run at start) |
| Backup | `docker compose exec postgres pg_dump -U postgres -Fc eullm > eullm-$(date +%F).dump` |
| Restore | `docker compose exec -T postgres pg_restore -U postgres -d eullm --clean < file.dump` |
| Stop | `docker compose down` (data stays in the `pgdata` volume) |

Each tenant can use its own Core token, so the Core counts and limits
its usage apart: add a token to `core.yaml` (`token_env: CORE_TOKEN_ACME`),
the variable to the `core` and Editor services in `compose.yaml`, and
`editor tenant-plan acme --core-token-env CORE_TOKEN_ACME`.
