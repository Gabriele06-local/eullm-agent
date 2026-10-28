# Backup and restore

One dated archive holds everything needed to bring the installation back,
here or on a new server: `backup.sh` writes it while everything keeps
running, cron runs it every night, and the restore takes five commands.
Commands run as root in the installation folder (`/opt/eullm-server`,
`~/eullm-server` or `deploy/server`).

## What is saved

| In the archive | What it holds | Why it matters |
|---|---|---|
| `eullm.dump` | The whole database: newsrooms, sites, profiles, sources, news, proposals, drafts, accounts, customers, payments | The service's content |
| `.env` | Database passwords, `EDITOR_SECRET_KEY`, model keys, SMTP, Mollie, `EULLM_VERSION` | Without `EDITOR_SECRET_KEY` the publishing credentials stored in the database cannot be read |
| `core.yaml` | The Core: model, spare models, limits | To start again with the same model |
| `compose.yaml`, `initdb/` | The services and the script that creates roles and database | To recreate the same installation |

The Docker images are not in it: they are pulled again for the
`EULLM_VERSION` of `.env`. The Core's workspace volume is not saved either.
Every archive holds every key: it is written with mode 600 and must leave
the server only for a private place, encrypted when the place is not yours.

## The script

[backup.sh](backup.sh) sits next to `compose.yaml`; in an installation made
by `server-init` fetch it once:

    curl -L -o backup.sh https://raw.githubusercontent.com/eullm/eullm-agent/main/deploy/server/backup.sh && chmod 700 backup.sh

It writes `<name>-YYYY-MM-DD_HHMM.tgz` and deletes
the archives older than the days to keep. Settings, all optional:

| Variable | Default | Example |
|---|---|---|
| `BACKUP_DIR` | `./backup` | `/home/app/backup`, a folder the hosting panel backs up too |
| `BACKUP_OWNER` | unchanged | `app:app`, the owner of that folder |
| `BACKUP_KEEP` | `14` | `30` |
| `BACKUP_NAME` | `eullm` | `redazia` |

The database is dumped first: if the dump fails the script stops and
writes no archive, and cron mails the error to root when root has a
mailbox.

## Automatic backup

Every night at 3:30, from `crontab -e` as root:

    30 3 * * * BACKUP_DIR=/home/app/backup BACKUP_OWNER=app:app /opt/eullm-server/backup.sh >/dev/null

Check it with `ls -lh` of the folder: an archive with today's date. In
October 2026 an archive of a busy installation is about 20 MB, so 14 of
them take about 300 MB.

## Manual backup

The same script, at any time, without stopping anything:

    ./backup.sh

Always before an update, a restore, a change to `.env` or `core.yaml`, or
work on the server. Backup and update in one line:

    ./backup.sh && sed -i 's/^EULLM_VERSION=.*/EULLM_VERSION=X.Y.Z/' .env && docker compose pull && docker compose up -d

A manual archive carries the time in its name, so it never replaces the
nightly one; it is deleted after the same number of days.

## A copy off the server

An archive that stays on the server does not survive a broken disk or a
compromised server: at least one copy should leave it every day.

- **Hosting panel.** With `BACKUP_DIR` inside a domain's home (Virtualmin:
  Backup and Restore → Scheduled Backups, home directory included), the
  panel's own backup carries the archives. Schedule it after 3:30.
- **Another server.** `0 5 * * * rsync -a /home/app/backup/ user@other-server:/backup/eullm/`
  with an SSH key that may only write in that folder.
- **Encryption** for places that are not yours:

      gpg --batch --pinentry-mode loopback --passphrase-file /root/.backup-pass -c eullm-YYYY-MM-DD_HHMM.tgz

  `gpg -d` decrypts it. Keep the passphrase off the server as well
  (a password manager): without it the copy cannot be opened.

## Restore on the same server

When the data went wrong or a given day is wanted back. Only the database
is replaced; `.env` and the configuration stay as they are. Whatever
happened after the archive's time is lost, so the first step saves the
present state.

1. Save the present state and pick the archive:

       ./backup.sh && ls -lh backup

2. Unpack the chosen archive:

       R=$(mktemp -d) && tar xzf backup/eullm-YYYY-MM-DD_HHMM.tgz -C "$R"

3. Stop the services, Postgres stays on:

       docker compose stop core editor-web editor-worker

4. Recreate the empty database with the installation's grants:

       docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE eullm WITH (FORCE)" -c "CREATE DATABASE eullm" -c "GRANT CONNECT, CREATE ON DATABASE eullm TO core, editor_owner" -c "GRANT CONNECT ON DATABASE eullm TO editor_web"
       docker compose exec -T postgres psql -U postgres -d eullm -c "GRANT CREATE ON SCHEMA public TO core, editor_owner"

5. Load the dump and start again:

       docker compose exec -T postgres pg_restore -U postgres -d eullm --exit-on-error < "$R/eullm.dump" && echo RESTORE-OK && docker compose up -d && rm -rf "$R"

Without `RESTORE-OK` do not go on: repeat steps 4 and 5 with the archive of
step 1. An archive of an older release is fine, `editor-migrate` brings the
database up to date at start; an archive of a newer release is not loaded
into an older installation.

## Restore on a new server

After losing the server, or to move. The new server needs Docker with the
compose plugin, a reverse proxy and the public name pointed at it.

1. Copy the archive there (`gpg -d` first if it is encrypted) and unpack it:

       mkdir -p /opt/eullm-server && cd /opt/eullm-server && tar xzf /path/eullm-YYYY-MM-DD_HHMM.tgz && chmod 600 .env

2. Start Postgres alone. On its first start it creates roles and database
   from `initdb/`, with the passwords of `.env`:

       docker compose pull && docker compose up -d postgres && sleep 15 && docker compose ps

3. Load the dump, once Postgres is `healthy`:

       docker compose exec -T postgres pg_restore -U postgres -d eullm --exit-on-error < eullm.dump && echo RESTORE-OK

4. Start the rest: `editor-migrate` ends `exited (0)`, the others `running`.

       docker compose up -d && docker compose ps

5. Put the reverse proxy back in front of `http://127.0.0.1:8090/`, passing
   the host name and the scheme (see [HTTPS](README.md#https)). With Apache:

       ProxyPreserveHost On
       RequestHeader set X-Forwarded-Proto "https"

6. Put `backup.sh` and its cron line back.

An error at step 3 means the Postgres volume was not new: stop there. When
the public name changes, set `EDITOR_PUBLIC_URL` in `.env` before step 4,
then the Mollie webhook and the sales site's backoffice address.

## Checks, and a monthly test

A backup is worth what its last restore proved. After a restore, and once
a month as a test:

- [ ] `/admin` signs you in with your usual account.
- [ ] Console → Newsrooms shows the sites, active sources and drafts of the
      archive's day.
- [ ] Desk: sites, proposals and drafts are there; Activity starts the
      collection at the next hourly round.
- [ ] A saved publishing destination opens without errors: `EDITOR_SECRET_KEY`
      is the right one.
- [ ] A test invite arrives by email: the SMTP settings work.
- [ ] `docker compose logs --since 10m editor-worker core` shows no
      repeated errors.

The monthly test leaves the server alone: on another computer with Docker,
follow "Restore on a new server" up to step 3, then start everything but the
worker:

    docker compose up -d core editor-migrate editor-web

Without the worker there is no hourly round: the test copy sends no email,
publishes nothing and spends nothing on the model. Open
`http://127.0.0.1:8090`, check the data, then remove it with
`docker compose down -v`.

Never run two full installations on the same data: the hourly round would
send briefings, emails and publications twice.
