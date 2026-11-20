"""Table definitions of the editor schema. The schema itself comes from the
migrations: Alembic for PostgreSQL, migrations_sqlite/ for SQLite. On SQLite
JSONB and arrays are stored as JSON text and times as UTC text."""

from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    REAL,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import JSONB as _JSONB


class _UtcDateTime(TypeDecorator):
    """SQLite has no time zones: store UTC, read back aware datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):  # an ISO time, as PostgreSQL accepts it
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=UTC)


JSONB = _JSONB().with_variant(JSON(), "sqlite")


def _array(item):
    return ARRAY(item).with_variant(JSON(), "sqlite")


def _ts():
    return DateTime(timezone=True).with_variant(_UtcDateTime(), "sqlite")


metadata = MetaData(schema="editor")


def _tenant() -> Column:
    return Column("tenant_id", Text, nullable=False)


tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("created_at", _ts()),
    Column("plan", Text),
    Column("max_sites", Integer),
    Column("max_active_sources", Integer),
    Column("max_items_per_day", Integer),
    Column("max_llm_cost_month", Float),
    Column("max_drafts_month", Integer),
    Column("max_languages", Integer),  # per site, its own included
    Column("timezone", Text),
    Column("briefing_hour", Integer),
    Column("language", Text),
    Column("core_token_env", Text),
    Column("active", Boolean),
    Column("external_ref", Text),
    Column("customer_id", BigInteger),
    Column("hold", Text),
    Column("autopilot", Boolean),  # the plan includes the automatic newsroom
    Column("evening", Boolean),  # the plan includes the evening analysis (Il Punto della Sera)
    Column("proposals_only", _ts()),  # since: trial or period over, proposals only
    Column("weekly_report", Boolean),  # the weekly report by email (on by default)
    Column("credits_month", Integer),  # credits the plan gives every month (credits.py)
    Column("max_users", Integer),  # people with access to the newsroom, the director included
    Column("credits_alert", Text),  # last alert sent: "YYYY-MM:80" or "YYYY-MM:100"
    Column("extra_languages", JSONB),  # languages bought beyond the plan, by site: {"<site id>": n}
)

sites = Table(
    "sites",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("domain", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("language", Text),
    Column("languages", JSONB),  # the languages it also publishes in, by translation
    Column("gsc_property", Text),  # Google Search Console: sc-domain:example.com or https://example.com/
    Column("gsc_status", Text),  # the last check or reading: "ok" or what went wrong
    Column("gsc_checked_at", _ts()),
    Column("feedback", Boolean, nullable=False, default=False),  # readers may write to the newsroom (readers.py)
)

sources = Table(
    "sources",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("config", JSONB),
    Column("weight", REAL),
    Column("sites", _array(Text)),
    Column("site_id", BigInteger),
    Column("status", Text),
    Column("origin", Text),
    Column("evidence", JSONB),
    Column("evaluation", JSONB),
    Column("score", REAL),
    Column("evaluated_at", _ts()),
    Column("status_reason", Text),
    Column("status_changed_at", _ts()),
    Column("status_set_by", Text),
    Column("consecutive_errors", Integer),
    Column("last_item_at", _ts()),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("last_fetched_at", _ts()),
    Column("last_error", Text),
    # An archive source (read once for every newsroom) or a newsroom's
    # source that follows one.
    Column("shared", Boolean),
    Column("archive_id", BigInteger),
)

source_items = Table(
    "source_items",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("source_id", BigInteger, ForeignKey("editor.sources.id"), nullable=False),
    Column("external_id", Text),
    Column("url", Text, nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("url_hash", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("author", Text),
    Column("published_at", _ts()),
    Column("fetched_at", _ts()),
    Column("metrics", JSONB),
    Column("minhash", _array(BigInteger), nullable=False),
    Column("duplicate_of", BigInteger),
    Column("content", Text),
    Column("content_fetched_at", _ts()),
)

observations = Table(
    "observations",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("item_id", BigInteger, nullable=False),
    Column("observed_at", _ts()),
    Column("metrics", JSONB, nullable=False),
)

topics = Table(
    "topics",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("label", Text, nullable=False),
    Column("keywords", _array(Text)),
    Column("summary", Text),
    Column("labelled_by", Text),
    Column("created_at", _ts()),
    Column("updated_at", _ts()),
    Column("labelled_items", Integer),  # its size when the model named it
)

topic_items = Table(
    "topic_items",
    metadata,
    _tenant(),
    Column("topic_id", BigInteger, primary_key=True),
    Column("item_id", BigInteger, primary_key=True),
)

trend_snapshots = Table(
    "trend_snapshots",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("taken_at", _ts()),
    Column("topics", JSONB, nullable=False),
)

trend_scores = Table(
    "trend_scores",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("topic_id", BigInteger, nullable=False),
    Column("computed_at", _ts()),
    Column("formula_version", Text, nullable=False),
    Column("score", REAL, nullable=False),
    Column("components", JSONB, nullable=False),
    Column("missing", _array(Text)),
)

llm_usage = Table(
    "llm_usage",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("purpose", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost", Float),
    Column("at", _ts()),
    Column("site_id", BigInteger),
    Column("ref", Text),
    Column("job_id", BigInteger),
    Column("duration_ms", Integer),
    Column("error", Text),
)

# Reference price of a model, in euro per million tokens, set in the console
# (no tenant: only the owner connection reads it).
model_prices = Table(
    "model_prices",
    metadata,
    Column("model", Text, primary_key=True),
    Column("input_per_mtok", Float, nullable=False),
    Column("output_per_mtok", Float, nullable=False),
    Column("note", Text, nullable=False, default=""),
    Column("updated_at", _ts()),
)

# Which model of core.yaml each step uses (no row: the Core's default); set
# in the console, read by every newsroom.
model_routes = Table(
    "model_routes",
    metadata,
    Column("purpose", Text, primary_key=True),
    Column("model", Text, nullable=False),
    Column("updated_at", _ts()),
)

# Installations that asked app.redazia.com for the latest release
# (updates.py, installs.py): a random identifier and the installed version.
installations = Table(
    "installations",
    metadata,
    Column("id", Text, primary_key=True),
    Column("version", Text, nullable=False),
    Column("first_seen", _ts()),
    Column("last_seen", _ts()),
    Column("checks", Integer, nullable=False, default=1),
)

# Settings of the installation entered in /admin › Impostazioni
# (appsettings.py); the secret ones encrypted with EDITOR_SECRET_KEY.
app_settings = Table(
    "app_settings",
    metadata,
    Column("name", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("secret", Boolean, nullable=False, default=False),
    Column("key_id", Text),
    Column("updated_at", _ts()),
    Column("updated_by", Text),
)

editorial_profiles = Table(
    "editorial_profiles",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("created_at", _ts()),
    Column("approved_by", Text),
    Column("approved_at", _ts()),
    Column("origin", Text),
    Column("analysis_id", BigInteger),
    Column("based_on", Integer),
    Column("changes", JSONB),
)

opportunity_scores = Table(
    "opportunity_scores",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("topic_id", BigInteger, nullable=False),
    Column("site_id", BigInteger, nullable=False),
    Column("profile_id", BigInteger, nullable=False),
    Column("computed_at", _ts()),
    Column("formula_version", Text, nullable=False),
    Column("score", REAL, nullable=False),
    Column("components", JSONB, nullable=False),
    Column("missing", _array(Text)),
)

proposals = Table(
    "proposals",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("topic_id", BigInteger, nullable=False),
    Column("profile_id", BigInteger, nullable=False),
    Column("opportunity", REAL, nullable=False),
    Column("title", Text, nullable=False),
    Column("angle", Text),
    Column("why_now", Text),
    Column("format", Text),
    Column("generated_by", Text, nullable=False),
    Column("status", Text),
    Column("decided_by", Text),
    Column("created_at", _ts()),
    Column("basis", JSONB),
)

proposal_citations = Table(
    "proposal_citations",
    metadata,
    _tenant(),
    Column("proposal_id", BigInteger, primary_key=True),
    Column("item_id", BigInteger, primary_key=True),
)

briefings = Table(
    "briefings",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("briefing_date", Date, nullable=False),
    Column("created_at", _ts()),
    Column("body_md", Text, nullable=False),
    Column("body_html", Text, nullable=False),
    Column("proposal_ids", _array(BigInteger)),
    Column("sent_email_at", _ts()),
    Column("sent_telegram_at", _ts()),
    Column("send_error", Text),
)

weekly_reports = Table(
    "weekly_reports",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("week_start", Date, nullable=False),
    Column("body_md", Text, nullable=False),
    Column("body_html", Text, nullable=False),
    Column("sent_to", JSONB),
    Column("sent_at", _ts()),
    Column("send_error", Text),
    Column("created_at", _ts()),
)

search_stats = Table(
    "search_stats",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("day", Date, nullable=False),
    Column("kind", Text, nullable=False),  # page or query
    Column("key", Text, nullable=False),
    Column("clicks", Integer, nullable=False),
    Column("impressions", Integer, nullable=False),
    Column("position", REAL),
)

recipients = Table(
    "recipients",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("channel", Text, nullable=False),
    Column("address", Text, nullable=False),
    Column("enabled", Boolean),
)

site_posts = Table(
    "site_posts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("url", Text, nullable=False),
    Column("url_hash", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("categories", _array(Text)),
    Column("published_at", _ts()),
    Column("first_seen_at", _ts()),
    Column("minhash", _array(BigInteger), nullable=False),
)

site_analyses = Table(
    "site_analyses",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("started_at", _ts()),
    Column("status", Text, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("problems", _array(Text)),
    Column("pages_fetched", Integer),
)

profile_reviews = Table(
    "profile_reviews",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("reviewed_at", _ts()),
    Column("analysis_id", BigInteger),
    Column("drift", JSONB, nullable=False),
    Column("significant", Boolean, nullable=False),
    Column("proposed_profile_id", BigInteger),
)

feedback = Table(
    "feedback",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger),
    Column("target", Text, nullable=False),
    Column("target_id", BigInteger, nullable=False),
    Column("verdict", Text, nullable=False),
    Column("note", Text),
    Column("given_by", Text, nullable=False),
    Column("given_at", _ts()),
)

drafts = Table(
    "drafts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("proposal_id", BigInteger, nullable=False),
    Column("site_id", BigInteger, nullable=False),
    Column("version", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("subtitle", Text),
    Column("body_md", Text, nullable=False),
    Column("language", Text),
    Column("status", Text),
    Column("flags", JSONB),
    Column("seo", JSONB),
    Column("translation_of", BigInteger),  # the draft this one translates
    Column("generated_by", Text, nullable=False),
    Column("created_at", _ts()),
    Column("decided_by", Text),
    Column("decided_at", _ts()),
)

draft_images = Table(
    "draft_images",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("draft_id", BigInteger, nullable=False),
    Column("token", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("alt", Text),
    Column("model", Text, nullable=False),
    Column("mime", Text, nullable=False),
    Column("width", Integer),
    Column("height", Integer),
    Column("content", LargeBinary, nullable=False),
    Column("created_at", _ts()),
    # A translation shows its original's image: the bytes stay on the
    # original's row (content is empty here), not one copy per language.
    Column("same_as", BigInteger),
)

feeds = Table(
    "feeds",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("token", Text, nullable=False),
    Column("created_at", _ts()),
)

draft_claims = Table(
    "draft_claims",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("draft_id", BigInteger, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("section", Text),
    Column("text", Text, nullable=False),
)

draft_claim_sources = Table(
    "draft_claim_sources",
    metadata,
    _tenant(),
    Column("claim_id", BigInteger, primary_key=True),
    Column("item_id", BigInteger, primary_key=True),
)

publish_targets = Table(
    "publish_targets",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("config", JSONB),
    Column("secret_env", Text),
    Column("enabled", Boolean),
    Column("created_at", _ts()),
)

publications = Table(
    "publications",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("draft_id", BigInteger, nullable=False),
    Column("target_id", BigInteger, nullable=False),
    Column("mode", Text),
    Column("payload", JSONB, nullable=False),
    Column("status", Text),
    Column("requested_by", Text, nullable=False),
    Column("requested_at", _ts()),
    Column("decided_by", Text),
    Column("decided_at", _ts()),
    Column("decision_note", Text),
    Column("published_at", _ts()),
    Column("external_id", Text),
    Column("external_url", Text),
    Column("error", Text),
    Column("attempts", Integer),
    Column("updated_at", _ts()),
    Column("updated_by", Text),
    Column("withdrawn_at", _ts()),
    Column("withdrawn_by", Text),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("kind", Text, nullable=False),
    Column("subject", Text),
    Column("status", Text),
    Column("error", Text),
    Column("started_at", _ts()),
    Column("finished_at", _ts()),
    Column("args", JSONB),
    Column("priority", Integer),
    Column("run_after", _ts()),
    Column("attempts", Integer),
    Column("max_attempts", Integer),
    Column("heartbeat_at", _ts()),
    Column("worker", Text),
    Column("progress", JSONB),  # the step a running job is on, see progress.py
    Column("result", JSONB),  # what the job produced, shown when it ends
)

access_tokens = Table(
    "access_tokens",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("name", Text, nullable=False),
    Column("token_sha256", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("created_at", _ts()),
    Column("revoked_at", _ts()),
)

secrets = Table(
    "secrets",
    metadata,
    _tenant(),
    Column("name", Text, primary_key=True),
    Column("ciphertext", Text, nullable=False),
    Column("key_id", Text, nullable=False),
    Column("updated_at", _ts()),
)

# Accounts (owner connection only; see migration 0014).

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("email", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("password_hash", Text),
    Column("operator", Boolean, nullable=False),
    Column("failed_logins", Integer, nullable=False),
    Column("locked_until", _ts()),
    Column("created_at", _ts()),
    Column("disabled_at", _ts()),
)

memberships = Table(
    "memberships",
    metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("tenant_id", Text, primary_key=True),
    Column("role", Text, nullable=False),
    Column("created_at", _ts()),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token_sha256", Text, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("tenant_id", Text),
    Column("created_at", _ts()),
    Column("expires_at", _ts(), nullable=False),
)

invites = Table(
    "invites",
    metadata,
    Column("token_sha256", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("tenant_id", Text),
    Column("role", Text),
    Column("operator", Boolean, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", _ts()),
    Column("expires_at", _ts(), nullable=False),
    Column("used_at", _ts()),
)

operator_tokens = Table(
    "operator_tokens",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", Text, nullable=False),
    Column("token_sha256", Text, nullable=False),
    Column("created_at", _ts()),
    Column("revoked_at", _ts()),
    Column("reseller_id", BigInteger),
    Column("last_used_at", _ts()),
)

# Customers, resellers, plans, subscriptions, payments (owner connection
# only; see migration 0015).

resellers = Table(
    "resellers",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("discount_pct", Float, nullable=False),
    Column("notes", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", _ts()),
)

customers = Table(
    "customers",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("reseller_id", BigInteger),
    Column("external_ref", Text),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("phone", Text, nullable=False),
    Column("vat_number", Text, nullable=False),
    Column("tax_code", Text, nullable=False),
    Column("address", Text, nullable=False),
    Column("postal_code", Text, nullable=False),
    Column("city", Text, nullable=False),
    Column("province", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("sdi_code", Text, nullable=False),
    Column("pec", Text, nullable=False),
    Column("provider_customer_id", Text),
    Column("created_at", _ts()),
    Column("updated_at", _ts()),
)

plans = Table(
    "plans",
    metadata,
    Column("code", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("price_month_cents", Integer),
    Column("price_year_cents", Integer),
    Column("currency", Text, nullable=False),
    Column("trial_days", Integer, nullable=False),
    Column("max_sites", Integer),
    Column("max_languages", Integer),
    Column("max_active_sources", Integer),
    Column("max_items_per_day", Integer),
    Column("max_drafts_month", Integer),
    Column("max_llm_cost_month", Float),
    Column("public", Boolean, nullable=False),
    Column("sort", Integer, nullable=False),
    Column("autopilot", Boolean, nullable=False),
    Column("credits_month", Integer),
    Column("evening", Boolean, nullable=False),
    Column("max_users", Integer),  # accesses included: people in the newsroom, the director included
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("customer_id", BigInteger),
    Column("plan_code", Text, nullable=False),
    Column("billing_interval", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_subscription_id", Text),
    Column("renewal_cents", Integer),  # what the gateway charges each renewal, VAT included (0.34.2)
    Column("trial_ends_at", _ts()),
    Column("current_period_end", _ts()),
    Column("reminded_at", _ts()),
    Column("desired_plan", Text),
    Column("desired_interval", Text),
    Column("created_at", _ts()),
    Column("updated_at", _ts()),
)

payments = Table(
    "payments",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tenant_id", Text),
    Column("subscription_id", BigInteger),
    Column("provider", Text, nullable=False),
    Column("provider_payment_id", Text),
    Column("kind", Text, nullable=False),
    Column("amount_cents", Integer, nullable=False),
    Column("vat_cents", Integer),  # the VAT inside amount_cents, for payments by card (0.34.2)
    Column("currency", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("created_at", _ts()),
    Column("paid_at", _ts()),
)

# Add-ons sold with the plan (billing.py): only "language", a language more
# for one site, for now.
addons = Table(
    "addons",
    metadata,
    Column("code", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("price_month_cents", Integer),
    Column("price_year_cents", Integer),
    Column("currency", Text, nullable=False),
)

# Credits (credits.py): the price list and the packs, from the console;
# every movement of a newsroom's credits.
credit_prices = Table(
    "credit_prices",
    metadata,
    Column("action", Text, primary_key=True),  # article, translation, image
    Column("credits", Integer, nullable=False),
)

credit_packs = Table(
    "credit_packs",
    metadata,
    Column("code", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("credits", Integer, nullable=False),
    Column("price_cents", Integer, nullable=False),
    Column("currency", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("sort", Integer, nullable=False),
)

credit_ledger = Table(
    "credit_ledger",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("at", _ts()),
    Column("pool", Text, nullable=False),  # month: the plan's credits; extra: packs and grants
    Column("delta", Integer, nullable=False),  # + added, - spent, 0 a free cover
    Column("reason", Text, nullable=False),  # article, translation, image, plan, pack, grant
    Column("ref", Text),  # draft:N, payment:N
    Column("note", Text, nullable=False, default=""),
    Column("by_user", Text),
)

# The free trial, once (trials.py): hashes of the data a trial was given
# to, and the second trials that were not.
trial_marks = Table(
    "trial_marks",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("value_hash", Text, nullable=False),
    Column("newsroom", Text, nullable=False),
    Column("customer_id", BigInteger),
    Column("created_at", _ts()),
)

trial_refusals = Table(
    "trial_refusals",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("newsroom", Text, nullable=False),
    Column("customer_id", BigInteger),
    Column("reasons", JSONB, nullable=False),
    Column("created_at", _ts()),
)


# Readers' messages from the form under the articles (readers.py): private,
# never sent to the model; pending until the reader confirms by email.
reader_messages = Table(
    "reader_messages",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _tenant(),
    Column("site_id", BigInteger, nullable=False),
    Column("url", Text, nullable=False, default=""),
    Column("title", Text, nullable=False, default=""),
    Column("kind", Text, nullable=False),  # error, comment, other
    Column("message", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("language", Text),
    Column("status", Text, nullable=False),  # pending, new, read, closed
    Column("token_sha256", Text),
    Column("address_hash", Text),
    Column("created_at", _ts()),
    Column("confirmed_at", _ts()),
)
