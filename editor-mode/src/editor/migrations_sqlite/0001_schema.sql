-- Editor Mode schema for the local installation (SQLite), equal to the
-- PostgreSQL schema at Alembic revision 0010 without what only a server
-- with several tenants needs: roles, row level security, the functions
-- that read across tenants. A later Alembic revision that changes tables
-- needs a numbered file here too.
--
-- Times are UTC text 'YYYY-MM-DD HH:MM:SS.ffffff' (what SQLAlchemy writes),
-- arrays and JSONB are JSON text.

CREATE TABLE editor.tenants (
    tenant_id text PRIMARY KEY,
    name text NOT NULL,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    plan text NOT NULL DEFAULT 'standard',
    max_sites integer,
    max_active_sources integer,
    max_items_per_day integer,
    max_llm_cost_month real,
    max_drafts_month integer,
    timezone text NOT NULL DEFAULT 'Europe/Rome',
    briefing_hour integer NOT NULL DEFAULT 8 CHECK (briefing_hour BETWEEN 0 AND 23),
    language text NOT NULL DEFAULT 'it',
    core_token_env text,
    active integer NOT NULL DEFAULT 1
);

-- One tenant per file: nothing in the local installation isolates tenants.
CREATE TRIGGER editor.tenants_one BEFORE INSERT ON tenants
WHEN EXISTS (SELECT 1 FROM tenants WHERE tenant_id <> NEW.tenant_id)
BEGIN
    SELECT RAISE(ABORT, 'the local installation has one tenant: use PostgreSQL for several');
END;

CREATE TABLE editor.sites (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    domain text NOT NULL,
    name text NOT NULL,
    language text NOT NULL DEFAULT 'it',
    UNIQUE (tenant_id, domain)
);

CREATE TABLE editor.sources (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    kind text NOT NULL CHECK (kind IN ('rss', 'hackernews', 'github', 'huggingface', 'arxiv')),
    name text NOT NULL,
    url text NOT NULL,
    config text NOT NULL DEFAULT '{}',
    weight real NOT NULL DEFAULT 1.0,
    sites text NOT NULL DEFAULT '[]',
    etag text,
    last_modified text,
    last_fetched_at text,
    last_error text,
    site_id integer REFERENCES sites ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('candidate', 'active', 'suspended', 'rejected')),
    origin text NOT NULL DEFAULT 'manual'
        CHECK (origin IN ('manual', 'site_outbound', 'site_feed', 'autodiscovery', 'api_query', 'model_suggestion')),
    evidence text NOT NULL DEFAULT '{}',
    evaluation text NOT NULL DEFAULT '{}',
    score real,
    evaluated_at text,
    status_reason text,
    status_changed_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    consecutive_errors integer NOT NULL DEFAULT 0,
    last_item_at text,
    status_set_by text
);
CREATE UNIQUE INDEX editor.sources_unique ON sources (tenant_id, coalesce(site_id, 0), kind, url, config);

CREATE TABLE editor.source_items (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    source_id integer NOT NULL REFERENCES sources ON DELETE CASCADE,
    external_id text,
    url text NOT NULL,
    canonical_url text NOT NULL,
    url_hash text NOT NULL,
    content_hash text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL DEFAULT '',
    author text,
    published_at text,
    fetched_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    metrics text NOT NULL DEFAULT '{}',
    minhash text NOT NULL,
    duplicate_of integer REFERENCES source_items,
    content text,
    content_fetched_at text,
    UNIQUE (tenant_id, url_hash)
);
CREATE INDEX editor.source_items_published ON source_items (tenant_id, published_at DESC);
CREATE INDEX editor.source_items_content ON source_items (tenant_id, content_hash);

CREATE TABLE editor.observations (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    item_id integer NOT NULL REFERENCES source_items ON DELETE CASCADE,
    observed_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    metrics text NOT NULL
);
CREATE INDEX editor.observations_item ON observations (item_id, observed_at);

CREATE TABLE editor.topics (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    label text NOT NULL,
    keywords text NOT NULL DEFAULT '[]',
    summary text NOT NULL DEFAULT '',
    labelled_by text NOT NULL DEFAULT 'keywords',
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    updated_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);

CREATE TABLE editor.topic_items (
    tenant_id text NOT NULL REFERENCES tenants,
    topic_id integer NOT NULL REFERENCES topics ON DELETE CASCADE,
    item_id integer NOT NULL REFERENCES source_items ON DELETE CASCADE,
    PRIMARY KEY (topic_id, item_id)
);
CREATE UNIQUE INDEX editor.topic_items_one_topic ON topic_items (item_id);

CREATE TABLE editor.trend_scores (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    topic_id integer NOT NULL REFERENCES topics ON DELETE CASCADE,
    computed_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    formula_version text NOT NULL,
    score real NOT NULL CHECK (score BETWEEN 0 AND 100),
    components text NOT NULL,
    missing text NOT NULL DEFAULT '[]'
);
CREATE INDEX editor.trend_scores_topic ON trend_scores (topic_id, computed_at DESC);

CREATE TABLE editor.llm_usage (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    purpose text NOT NULL,
    model text NOT NULL,
    input_tokens integer,
    output_tokens integer,
    cost real,
    at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);

CREATE TABLE editor.site_analyses (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    started_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    status text NOT NULL CHECK (status IN ('complete', 'partial', 'insufficient', 'failed')),
    snapshot text NOT NULL,
    problems text NOT NULL DEFAULT '[]',
    pages_fetched integer NOT NULL DEFAULT 0
);

CREATE TABLE editor.editorial_profiles (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    version integer NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'retired')),
    body text NOT NULL,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    approved_by text,
    approved_at text,
    origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('analysis', 'reanalysis', 'manual')),
    analysis_id integer REFERENCES site_analyses,
    based_on integer,
    changes text NOT NULL DEFAULT '[]',
    UNIQUE (site_id, version),
    CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);
CREATE UNIQUE INDEX editor.editorial_profiles_one_approved
    ON editorial_profiles (site_id) WHERE status = 'approved';

CREATE TABLE editor.opportunity_scores (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    topic_id integer NOT NULL REFERENCES topics ON DELETE CASCADE,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    profile_id integer NOT NULL REFERENCES editorial_profiles,
    computed_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    formula_version text NOT NULL,
    score real NOT NULL CHECK (score BETWEEN 0 AND 100),
    components text NOT NULL,
    missing text NOT NULL DEFAULT '[]'
);
CREATE INDEX editor.opportunity_scores_site ON opportunity_scores (site_id, computed_at DESC);

CREATE TABLE editor.proposals (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    topic_id integer NOT NULL REFERENCES topics,
    profile_id integer NOT NULL REFERENCES editorial_profiles,
    opportunity real NOT NULL,
    title text NOT NULL,
    angle text NOT NULL DEFAULT '',
    why_now text NOT NULL DEFAULT '',
    format text NOT NULL DEFAULT 'article',
    generated_by text NOT NULL,
    status text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'drafted')),
    decided_by text,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
CREATE INDEX editor.proposals_site ON proposals (site_id, created_at DESC);

-- A proposal cites only items that exist: the foreign key enforces it.
CREATE TABLE editor.proposal_citations (
    tenant_id text NOT NULL REFERENCES tenants,
    proposal_id integer NOT NULL REFERENCES proposals ON DELETE CASCADE,
    item_id integer NOT NULL REFERENCES source_items,
    PRIMARY KEY (proposal_id, item_id)
);

CREATE TABLE editor.briefings (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    briefing_date text NOT NULL,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    body_md text NOT NULL,
    body_html text NOT NULL,
    proposal_ids text NOT NULL DEFAULT '[]',
    sent_email_at text,
    sent_telegram_at text,
    send_error text,
    UNIQUE (tenant_id, briefing_date)
);

CREATE TABLE editor.recipients (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    channel text NOT NULL CHECK (channel IN ('email', 'telegram')),
    address text NOT NULL,
    enabled integer NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, channel, address)
);

CREATE TABLE editor.site_posts (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    url text NOT NULL,
    url_hash text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL DEFAULT '',
    categories text NOT NULL DEFAULT '[]',
    published_at text,
    first_seen_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    minhash text NOT NULL,
    UNIQUE (site_id, url_hash)
);
CREATE INDEX editor.site_posts_published ON site_posts (site_id, published_at DESC);

CREATE TABLE editor.profile_reviews (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    reviewed_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    analysis_id integer REFERENCES site_analyses,
    drift text NOT NULL,
    significant integer NOT NULL,
    proposed_profile_id integer REFERENCES editorial_profiles
);

CREATE TABLE editor.feedback (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer REFERENCES sites ON DELETE CASCADE,
    target text NOT NULL CHECK (target IN ('proposal', 'topic', 'source', 'draft')),
    target_id integer NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('up', 'down', 'already_covered', 'off_topic')),
    note text,
    given_by text NOT NULL,
    given_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);

CREATE TABLE editor.access_tokens (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    name text NOT NULL,
    token_sha256 text NOT NULL UNIQUE
        CHECK (length(token_sha256) = 64 AND token_sha256 NOT GLOB '*[^0-9a-f]*'),
    role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    revoked_at text
);

CREATE TABLE editor.drafts (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    proposal_id integer NOT NULL REFERENCES proposals ON DELETE CASCADE,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    version integer NOT NULL,
    title text NOT NULL,
    subtitle text NOT NULL DEFAULT '',
    body_md text NOT NULL,
    language text,
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'needs_review', 'approved', 'rejected')),
    flags text NOT NULL DEFAULT '[]',
    generated_by text NOT NULL,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    decided_by text,
    decided_at text,
    UNIQUE (proposal_id, version),
    CHECK (status NOT IN ('approved', 'rejected') OR decided_by IS NOT NULL)
);

CREATE TABLE editor.draft_claims (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    draft_id integer NOT NULL REFERENCES drafts ON DELETE CASCADE,
    ordinal integer NOT NULL,
    section text,
    text text NOT NULL,
    UNIQUE (draft_id, ordinal)
);

-- Every claim has at least one source, and every source is a stored item.
CREATE TABLE editor.draft_claim_sources (
    tenant_id text NOT NULL REFERENCES tenants,
    claim_id integer NOT NULL REFERENCES draft_claims ON DELETE CASCADE,
    item_id integer NOT NULL REFERENCES source_items,
    PRIMARY KEY (claim_id, item_id)
);

CREATE TABLE editor.publish_targets (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('wordpress', 'webhook', 'telegram_channel')),
    name text NOT NULL,
    -- Never secrets: credentials are read from the environment.
    config text NOT NULL DEFAULT '{}',
    secret_env text,
    enabled integer NOT NULL DEFAULT 1,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);

CREATE TABLE editor.publications (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    draft_id integer NOT NULL REFERENCES drafts ON DELETE CASCADE,
    target_id integer NOT NULL REFERENCES publish_targets ON DELETE CASCADE,
    mode text NOT NULL DEFAULT 'draft' CHECK (mode IN ('draft', 'publish')),
    payload text NOT NULL,
    status text NOT NULL DEFAULT 'pending_approval'
        CHECK (status IN ('pending_approval', 'approved', 'sending', 'rejected', 'published', 'failed')),
    requested_by text NOT NULL,
    requested_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    decided_by text,
    decided_at text,
    decision_note text,
    published_at text,
    external_id text,
    external_url text,
    error text,
    attempts integer NOT NULL DEFAULT 0,
    -- Nothing goes out without a person's decision.
    CHECK (status IN ('pending_approval') OR decided_by IS NOT NULL)
);
CREATE UNIQUE INDEX editor.publications_once ON publications (draft_id, target_id, mode)
    WHERE status IN ('pending_approval', 'approved', 'sending', 'published');

CREATE TABLE editor.jobs (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    kind text NOT NULL,
    subject text,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    error text,
    started_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    finished_at text
);
CREATE INDEX editor.jobs_recent ON jobs (tenant_id, kind, started_at DESC);
