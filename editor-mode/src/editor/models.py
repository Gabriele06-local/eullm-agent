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
    Column("currency", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("created_at", _ts()),
    Column("paid_at", _ts()),
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
