#!/bin/sh
# Runs once, when the PostgreSQL volume is new: one database, a role for the
# Core (schema core), an owner for the editor schema and its migrations, and
# the login Editor Mode uses, which row-level security always applies to.
set -eu
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE core LOGIN PASSWORD '$CORE_DB_PASSWORD';
CREATE ROLE editor_owner LOGIN PASSWORD '$EDITOR_OWNER_DB_PASSWORD';
CREATE ROLE editor_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE editor_web LOGIN PASSWORD '$EDITOR_WEB_DB_PASSWORD' IN ROLE editor_app;
CREATE DATABASE eullm;
GRANT CONNECT, CREATE ON DATABASE eullm TO core, editor_owner;
GRANT CONNECT ON DATABASE eullm TO editor_web;
\c eullm
-- Schema public keeps the Core's migration history (_sqlx_migrations) and
-- the job queue of the Editor Mode worker.
GRANT CREATE ON SCHEMA public TO core, editor_owner;
SQL
