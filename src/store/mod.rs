//! Run state and audit for the Core service.
//!
//! [`PgStore`] keeps everything in the `core` schema of PostgreSQL (server,
//! several tenants); [`SqliteStore`] in one SQLite file (local installation,
//! one user); [`MemoryStore`] in memory for tests and quick local use.

use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub mod memory;
pub mod postgres;
pub mod sqlite;

pub use memory::MemoryStore;
pub use postgres::PgStore;
pub use sqlite::SqliteStore;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    Queued,
    Running,
    WaitingApproval,
    Succeeded,
    Failed,
}

impl RunStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            RunStatus::Queued => "queued",
            RunStatus::Running => "running",
            RunStatus::WaitingApproval => "waiting_approval",
            RunStatus::Succeeded => "succeeded",
            RunStatus::Failed => "failed",
        }
    }

    pub fn parse(s: &str) -> Self {
        match s {
            "queued" => RunStatus::Queued,
            "running" => RunStatus::Running,
            "waiting_approval" => RunStatus::WaitingApproval,
            "succeeded" => RunStatus::Succeeded,
            _ => RunStatus::Failed,
        }
    }
}

#[derive(Debug, Clone)]
pub struct NewRun {
    pub id: String,
    pub tenant: String,
    pub profile: String,
    pub source: String,
    pub input: String,
}

/// How a run ended.
#[derive(Debug, Clone, Default)]
pub struct RunEnd {
    pub iterations: usize,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cost: Option<f64>,
    pub tainted: bool,
    pub output: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LlmCallRecord {
    pub provider: String,
    pub model: String,
    pub duration_ms: u64,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub cost: Option<f64>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolCallRecord {
    pub tool: String,
    /// Argument names only: values can hold personal data or secrets.
    pub argument_keys: Vec<String>,
    pub decision: String,
    pub decision_reason: Option<String>,
    pub approval_id: Option<String>,
    pub duration_ms: u64,
    pub output_bytes: Option<u64>,
    pub error: Option<String>,
}

/// One request made through `POST /v1/fetch`. The query string is left out
/// of the stored URL: it can carry keys.
#[derive(Debug, Clone, Serialize)]
pub struct FetchRecord {
    pub url: String,
    pub status: Option<u16>,
    pub bytes: u64,
    pub duration_ms: u64,
    pub error: Option<String>,
}

/// What a tenant used since a moment: the basis for limits and billing.
#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct Usage {
    pub runs: u64,
    pub llm_calls: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cost: f64,
    pub fetches: u64,
}

/// One step of a run, as returned by the API.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Step {
    LlmCall {
        at_ms: i64,
        #[serde(flatten)]
        call: LlmCallRecord,
    },
    ToolCall {
        at_ms: i64,
        #[serde(flatten)]
        call: ToolCallRecord,
    },
}

#[derive(Debug, Clone, Serialize)]
pub struct RunSummary {
    pub id: String,
    pub tenant: String,
    pub profile: String,
    pub source: String,
    pub status: RunStatus,
    pub created_at_ms: i64,
    pub finished_at_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RunView {
    #[serde(flatten)]
    pub summary: RunSummary,
    pub input: String,
    pub output: Option<String>,
    pub error: Option<String>,
    pub iterations: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cost: Option<f64>,
    pub tainted: bool,
    pub steps: Vec<Step>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalStatus {
    Pending,
    Approved,
    Denied,
    Expired,
}

impl ApprovalStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ApprovalStatus::Pending => "pending",
            ApprovalStatus::Approved => "approved",
            ApprovalStatus::Denied => "denied",
            ApprovalStatus::Expired => "expired",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "pending" => ApprovalStatus::Pending,
            "approved" => ApprovalStatus::Approved,
            "denied" => ApprovalStatus::Denied,
            "expired" => ApprovalStatus::Expired,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Approval {
    pub id: String,
    pub tenant: String,
    pub run_id: String,
    pub tool: String,
    /// Shown to the person deciding, so it holds the real values
    /// (truncated); unlike the audit, which keeps names only.
    pub arguments: Value,
    pub reason: String,
    pub status: ApprovalStatus,
    pub decided_by: Option<String>,
    pub decision_note: Option<String>,
    pub created_at_ms: i64,
    pub decided_at_ms: Option<i64>,
}

#[async_trait]
pub trait Store: Send + Sync {
    async fn create_run(&self, run: &NewRun) -> Result<()>;
    async fn set_run_status(&self, id: &str, status: RunStatus) -> Result<()>;
    async fn finish_run(&self, id: &str, end: &RunEnd) -> Result<()>;
    async fn get_run(&self, tenant: &str, id: &str) -> Result<Option<RunView>>;
    async fn list_runs(&self, tenant: &str, limit: u32) -> Result<Vec<RunSummary>>;
    /// `run_id` is `None` for single calls made through `/v1/llm/chat`.
    async fn record_llm_call(
        &self,
        tenant: &str,
        run_id: Option<&str>,
        call: &LlmCallRecord,
    ) -> Result<()>;
    async fn record_tool_call(
        &self,
        tenant: &str,
        run_id: &str,
        call: &ToolCallRecord,
    ) -> Result<()>;
    async fn record_fetch(&self, tenant: &str, fetch: &FetchRecord) -> Result<()>;
    /// Runs, model calls (inside runs or not) and fetches since `since_ms`.
    async fn usage(&self, tenant: &str, since_ms: i64) -> Result<Usage>;
    async fn create_approval(&self, approval: &Approval) -> Result<()>;
    /// Decide a pending approval; `None` when it does not exist for this
    /// tenant or was already decided.
    async fn decide_approval(
        &self,
        tenant: &str,
        id: &str,
        approve: bool,
        by: &str,
        note: Option<&str>,
    ) -> Result<Option<Approval>>;
    async fn expire_approval(&self, id: &str) -> Result<()>;
    async fn get_approval(&self, tenant: &str, id: &str) -> Result<Option<Approval>>;
    /// At startup: runs whose source starts with `source_prefix` (`api:`,
    /// `telegram:`) left queued, running or waiting by a previous process
    /// fail as interrupted, and their pending approvals expire. Assumes one
    /// process per kind of source on a database. Returns the runs closed.
    async fn recover_interrupted(&self, source_prefix: &str) -> Result<u64>;
    async fn list_approvals(
        &self,
        tenant: &str,
        status: Option<ApprovalStatus>,
    ) -> Result<Vec<Approval>>;
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
