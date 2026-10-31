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
`core.yaml` and `.env`; the data lives in Docker volumes. It asks the
model (NVIDIA Build, Groq, EuLLM Engine on this machine or another
OpenAI-compatible service) and its key, then **your administrator
account** (email and password, typed twice and not shown) and the name of
a first newsroom, then:

- writes the compose file, `core.yaml` and `.env` with every password and
  token generated (`.env` readable only by you; your password is not
  written anywhere);
- downloads the images of the same release and starts everything;
- creates your administrator account and the first newsroom, owned by it,
  and prints where to sign in: `/admin` for the administration, `/editor`
  for the newsroom.

Keep a copy of `.env`: `EDITOR_SECRET_KEY` in it encrypts the publishing
credentials. Running `server-init` again changes no file that exists and
asks nothing: it only starts the stack (and prints a first-start link if
there is still no administrator). In a checkout of the sources,
`--dir deploy/server` uses this folder. Without questions:
`--provider nvidia --model <name> --admin-email you@example.com
--newsroom "Redazione"`, the model key in `LLM_API_KEY` and the password
in `EDITOR_ADMIN_PASSWORD`.

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

4. Your administrator account. Either put it in `.env` before step 3 and
   it is created at the first start, with a first newsroom owned by it:

       EDITOR_ADMIN_EMAIL=you@example.com
       EDITOR_ADMIN_PASSWORD=at least 10 characters
       EDITOR_FIRST_NEWSROOM=Redazione

   (once you have signed in, empty `EDITOR_ADMIN_PASSWORD`: it is used
   only while no administrator exists); or leave them empty and open the
   first-start link the start prints, to create it from the browser:

       docker compose logs editor-migrate | grep setup
       docker compose exec editor-web editor setup-link

   Then sign in: `/admin` is the administration, `/editor` the newsroom.
   The Core's web interface at http://127.0.0.1:8088 opens with
   `CORE_ADMIN_TOKEN` from `.env`.

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

## Selling: plans, trial, payments, resellers

The public site (or a reseller) signs customers up through the same API;
the newsroom starts on the trial plan and stops by itself when the trial
ends without a payment. Its data stay, and its owner can still sign in to
`/account` to choose a plan and pay.

- **Plans** live in the console (`/admin` → Piani): prices without VAT,
  trial days, limits. The public site reads them, without a key, from
  `GET /api/public/plans`.
- **Signup** from the site:

      curl -X POST https://editor.example.com/api/operator/signup \
        -H "Authorization: Bearer edo_..." -H "Content-Type: application/json" \
        -d '{"external_ref": "web-123", "newsroom": "Rossi News",
             "customer": {"kind": "company", "name": "Rossi Media srl", "email": "amministrazione@rossi.it",
                          "vat_number": "01234567890", "address": "Via Roma 1", "postal_code": "00100",
                          "city": "Roma", "province": "RM", "country": "IT", "sdi_code": "ABC1234"}}'

  It answers 201 with the customer, the newsroom, its subscription
  (`trialing`, 7 days) and the owner's invite; the same `external_ref`
  again answers 200 and changes nothing. VAT number, tax code, SDI code
  and PEC are checked. The error messages (`{"error": ...}`, meant for the
  visitor) and the owner's invite email are written in `"language"` (`en`,
  `it`, `de`, `fr`, `es`) when the body has it, else in the request's
  `Accept-Language`; the newsroom gets the same language.
  The plan the visitor picked goes in `"plan"` (and `"interval"`): from
  the site's key a paid plan never starts without its payment, the
  newsroom starts on trial and the subscription keeps `desired_plan` and
  `desired_interval`, already selected when the owner opens `/account` to
  pay. A reseller's key starts the plan at once (the reseller bills it).
- **Customers**: `GET /api/operator/customers[?external_ref=...]`,
  `GET` and `PATCH /api/operator/customers/{id}`.
- **Plan of a newsroom**: `GET` and `PUT /api/operator/tenants/{id}/subscription`
  (`plan`, `interval` month or year, `paid_until`, and `amount_cents` to
  record a payment received by bank transfer), `POST .../cancel`.
- **Online payments** (Mollie): set `EDITOR_MOLLIE_API_KEY` in `.env`
  (a `test_` key first) and `EDITOR_PUBLIC_URL`. The owner chooses the
  plan in `/account` and pays; the first payment also authorises the
  renewals, which Mollie charges by itself. Mollie notifies
  `https://<EDITOR_PUBLIC_URL>/api/billing/mollie/webhook`, which must be
  reachable from the internet. `POST /api/operator/tenants/{id}/checkout`
  gives the payment address to the public site instead. Without a key,
  plans are paid by bank transfer and recorded from the console.
- **Cover images**: set `EDITOR_IMAGES_API_KEY` in `.env`. An OpenRouter
  key (`sk-or-...`) draws with `openai/gpt-image-2` (about 0.05 $ at
  `EDITOR_IMAGES_QUALITY=medium`, under 0.02 $ at `low`; another model with
  `EDITOR_IMAGES_MODEL=owner/name`); a Black Forest Labs key draws with
  FLUX on `api.eu.bfl.ai` (`flux-2-pro`). Each draft then gets an image
  drawn for it, kept in the database and served at `/media/<token>`;
  WordPress receives it as the featured image, a GitHub site in its
  repository, webhooks as `image.url` (an absolute address needs
  `EDITOR_PUBLIC_URL`). Each site chooses, in Sito e profilo, covers made
  to be clicked (the default: one bold subject, dramatic light, strong
  colours, readable as a thumbnail, optionally a 2-4 word headline drawn
  in) or plain editorial photos. The cost of each image is in Consumi. A
  `compose.yaml` written before 0.17.0 needs the `EDITOR_IMAGES_*` lines of
  [compose.yaml](compose.yaml) under `x-editor-env`.
- **What happens by itself** (every hour, in the worker): an email to the
  owner two days before the trial ends; the hold when the trial ends, or
  three days after a paid period ends without a renewal.
- **Resellers** (`/admin` → Rivenditori, or `POST /api/operator/resellers`):
  each one gets an API key bound to it. With that key it signs up and
  manages its own customers only, sets their plan with `PUT .../subscription`
  and bills them itself (no trial expiry unless it sets `paid_until`); it
  cannot see other customers, create resellers or change limits directly.

## HTTPS

A reverse proxy on the same machine, for example Caddy
(`/etc/caddy/Caddyfile`), gets and renews the certificates:

    editor.example.com {
        reverse_proxy 127.0.0.1:8090
    }

Only Editor Mode goes on the internet. The Core (8088) serves Editor Mode
inside Docker's network and the administrator: leave it on 127.0.0.1 and
reach its web interface through an SSH tunnel
(`ssh -L 8088:127.0.0.1:8088 server`, then http://127.0.0.1:8088). Keep
`BIND=127.0.0.1` in `.env`: Docker publishes ports past the firewall
(ufw), so a port on 0.0.0.0 would be open to everyone. Open only 22, 80
and 443.

## Updating

The release to run is `EULLM_VERSION` in `.env`: `server-init` writes the
version of its binary there, so it does not move on its own. When a new
release is out (its job on GitHub Actions has published the images), in
the installation folder:

    cd ~/eullm-server
    ./backup.sh                                                # see BACKUP.md
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

## Updating from the console

/admin › Aggiornamenti shows the installed version and the latest release
(read every six hours, or with "Controlla adesso"; see below), and with
"Aggiorna alla X.Y.Z" updates the server without a terminal. The dashboard
has no Docker socket and no root: it only writes the version asked in
`updates/request.json`. `update.sh`, run every minute by root's cron,
takes the request, runs `backup.sh`, pulls the images of that version
(retrying for 10 minutes while the release publishes them), writes it in
`.env`, restarts and waits for the dashboard to answer. How it went shows
in the console, with the log. It never goes back on its own: if the new
version does not answer, the backup made just before is in `backup/`.

Once, as root, in the installation folder (`server-init` writes
`update.sh`; an older installation downloads it first):

    cd /opt/eullm-server
    curl -fsSL -o update.sh https://raw.githubusercontent.com/eullm/eullm-agent/main/deploy/server/update.sh
    sudo sh update.sh install

`install` creates `updates/` (owned by the dashboard's user, uid 999),
adds `/etc/cron.d/eullm-update`, and, when `compose.yaml` comes from a
release before this one, writes `compose.override.yaml` with the shared
folder; then it restarts. The backups of `update.sh` use the `BACKUP_*`
settings of `backup.sh`: to keep them elsewhere, put them in the cron line.

### Counting installations

The latest release is asked to `https://app.redazia.com/api/public/latest`,
which answers with GitHub's latest release. Only two things are sent: a
random identifier derived from this installation's `EDITOR_SECRET_KEY`
(it cannot be turned back into the key) and the installed version. This is
how we know how many installations run and on which version; nothing about
newsrooms, sites, users or addresses is sent or kept. If that address does
not answer, GitHub is asked directly. To send nothing, put
`EDITOR_TELEMETRY=off` in `.env` (a `compose.yaml` written before 0.24.0
also needs the line `EDITOR_TELEMETRY: ${EDITOR_TELEMETRY:-on}` under
`x-editor-env`) and `docker compose up -d`.

On the server that receives the checks, /admin › Aggiornamenti shows the
installations active in the last 7 days, by version.

## A spare model

Free model services have rate limits and bad days. `fallback:` in
`core.yaml` names a spare model the Core uses when the main one fails (an
error, a rate limit, a timeout); a spare can have its own spare. The
commented block in [core.example.yaml](core.example.yaml) has Mistral
(European) and OpenRouter's free models. Uncomment it in `core.yaml`, put
the keys in `.env` as `LLM_FALLBACK_API_KEY` and `LLM_FALLBACK2_API_KEY`,
then `docker compose up -d`. The dashboard shows, bottom left, the model
that answered last.

A `compose.yaml` written before 0.7.7 does not pass those two keys to the
Core: add them under `core:` → `environment:` as in
[compose.yaml](compose.yaml).

## Day to day

| What | Command |
|---|---|
| Logs | `docker compose logs -f core editor-worker` |
| Update | see [Updating](#updating) |
| Try a branch | `git checkout <branch> && docker compose up -d --build`: compiles the Core and Editor Mode from the checkout |
| Backup | `./backup.sh`: one dated archive with the database, `.env` and the configuration; nightly with cron, see [BACKUP.md](BACKUP.md) |
| Restore | same server or a new one: [BACKUP.md](BACKUP.md) |
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
