use anyhow::{bail, Result};
use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::Mutex;

use super::*;

/// In-memory store: state is lost on restart.
#[derive(Default)]
pub struct MemoryStore {
    inner: Mutex<Inner>,
}

#[derive(Default)]
struct Inner {
    runs: HashMap<String, RunView>,
    order: Vec<String>,
    approvals: HashMap<String, Approval>,
    approval_order: Vec<String>,
    /// Single LLM calls outside a run, kept for completeness.
    loose_llm_calls: Vec<(String, LlmCallRecord)>,
    fetches: Vec<(String, FetchRecord)>,
    tenant_limits: HashMap<String, TenantLimits>,
    /// (tenant, at_ms, call) for every model call, for usage.
    llm_log: Vec<(String, i64, LlmCallRecord)>,
    fetch_log: Vec<(String, i64)>,
}

impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Calls made through `/v1/llm/chat`, for tests.
    pub fn loose_llm_calls(&self) -> Vec<(String, LlmCallRecord)> {
        self.inner.lock().unwrap().loose_llm_calls.clone()
    }

    /// Requests made through `/v1/fetch`, for tests.
    pub fn fetches(&self) -> Vec<(String, FetchRecord)> {
        self.inner.lock().unwrap().fetches.clone()
    }
}

#[async_trait]
impl Store for MemoryStore {
    async fn create_run(&self, run: &NewRun) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        if g.runs.contains_key(&run.id) {
            bail!("run {} already exists", run.id);
        }
        g.runs.insert(
            run.id.clone(),
            RunView {
                summary: RunSummary {
                    id: run.id.clone(),
                    tenant: run.tenant.clone(),
                    profile: run.profile.clone(),
                    source: run.source.clone(),
                    status: RunStatus::Queued,
                    created_at_ms: now_ms(),
                    finished_at_ms: None,
                },
                input: run.input.clone(),
                output: None,
                error: None,
                iterations: 0,
                input_tokens: 0,
                output_tokens: 0,
                cost: None,
                tainted: false,
                steps: Vec::new(),
            },
        );
        g.order.push(run.id.clone());
        Ok(())
    }

    async fn set_run_status(&self, id: &str, status: RunStatus) -> Result<()> {
        if let Some(r) = self.inner.lock().unwrap().runs.get_mut(id) {
            r.summary.status = status;
        }
        Ok(())
    }

    async fn finish_run(&self, id: &str, end: &RunEnd) -> Result<()> {
        if let Some(r) = self.inner.lock().unwrap().runs.get_mut(id) {
            r.summary.status = if end.error.is_some() {
                RunStatus::Failed
            } else {
                RunStatus::Succeeded
            };
            r.summary.finished_at_ms = Some(now_ms());
            r.iterations = end.iterations as u64;
            r.input_tokens = end.input_tokens;
            r.output_tokens = end.output_tokens;
            r.cost = end.cost;
            r.tainted = end.tainted;
            r.output = end.output.clone();
            r.error = end.error.clone();
        }
        Ok(())
    }

    async fn get_run(&self, tenant: &str, id: &str) -> Result<Option<RunView>> {
        let g = self.inner.lock().unwrap();
        Ok(g.runs
            .get(id)
            .filter(|r| r.summary.tenant == tenant)
            .cloned())
    }

    async fn list_runs(&self, tenant: &str, limit: u32) -> Result<Vec<RunSummary>> {
        let g = self.inner.lock().unwrap();
        Ok(g.order
            .iter()
            .rev()
            .filter_map(|id| g.runs.get(id))
            .filter(|r| r.summary.tenant == tenant)
            .take(limit as usize)
            .map(|r| r.summary.clone())
            .collect())
    }

    async fn record_llm_call(
        &self,
        tenant: &str,
        run_id: Option<&str>,
        call: &LlmCallRecord,
    ) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        g.llm_log.push((tenant.to_string(), now_ms(), call.clone()));
        match run_id.and_then(|id| g.runs.get_mut(id)) {
            Some(r) => r.steps.push(Step::LlmCall {
                at_ms: now_ms(),
                call: call.clone(),
            }),
            None => g.loose_llm_calls.push((tenant.to_string(), call.clone())),
        }
        Ok(())
    }

    async fn record_fetch(&self, tenant: &str, fetch: &FetchRecord) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        g.fetches.push((tenant.to_string(), fetch.clone()));
        g.fetch_log.push((tenant.to_string(), now_ms()));
        Ok(())
    }

    async fn usage(&self, tenant: &str, since_ms: i64) -> Result<Usage> {
        let g = self.inner.lock().unwrap();
        let mut u = Usage {
            runs: g
                .runs
                .values()
                .filter(|r| r.summary.tenant == tenant && r.summary.created_at_ms >= since_ms)
                .count() as u64,
            fetches: g
                .fetch_log
                .iter()
                .filter(|(t, at)| t == tenant && *at >= since_ms)
                .count() as u64,
            ..Usage::default()
        };
        for (_, _, c) in g
            .llm_log
            .iter()
            .filter(|(t, at, _)| t == tenant && *at >= since_ms)
        {
            u.llm_calls += 1;
            u.input_tokens += c.input_tokens.unwrap_or(0);
            u.output_tokens += c.output_tokens.unwrap_or(0);
            u.cost += c.cost.unwrap_or(0.0);
        }
        Ok(u)
    }

    async fn record_tool_call(
        &self,
        _tenant: &str,
        run_id: &str,
        call: &ToolCallRecord,
    ) -> Result<()> {
        if let Some(r) = self.inner.lock().unwrap().runs.get_mut(run_id) {
            r.steps.push(Step::ToolCall {
                at_ms: now_ms(),
                call: call.clone(),
            });
        }
        Ok(())
    }

    async fn create_approval(&self, approval: &Approval) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        if let Some(r) = g.runs.get_mut(&approval.run_id) {
            r.summary.status = RunStatus::WaitingApproval;
        }
        g.approvals.insert(approval.id.clone(), approval.clone());
        g.approval_order.push(approval.id.clone());
        Ok(())
    }

    async fn decide_approval(
        &self,
        tenant: &str,
        id: &str,
        approve: bool,
        by: &str,
        note: Option<&str>,
    ) -> Result<Option<Approval>> {
        let mut g = self.inner.lock().unwrap();
        let Some(a) = g.approvals.get_mut(id) else {
            return Ok(None);
        };
        if a.tenant != tenant || a.status != ApprovalStatus::Pending {
            return Ok(None);
        }
        a.status = if approve {
            ApprovalStatus::Approved
        } else {
            ApprovalStatus::Denied
        };
        a.decided_by = Some(by.to_string());
        a.decision_note = note.map(str::to_string);
        a.decided_at_ms = Some(now_ms());
        let a = a.clone();
        if let Some(r) = g.runs.get_mut(&a.run_id) {
            if r.summary.status == RunStatus::WaitingApproval {
                r.summary.status = RunStatus::Running;
            }
        }
        Ok(Some(a))
    }

    async fn expire_approval(&self, id: &str) -> Result<()> {
        if let Some(a) = self.inner.lock().unwrap().approvals.get_mut(id) {
            if a.status == ApprovalStatus::Pending {
                a.status = ApprovalStatus::Expired;
                a.decided_at_ms = Some(now_ms());
            }
        }
        Ok(())
    }

    async fn get_approval(&self, tenant: &str, id: &str) -> Result<Option<Approval>> {
        let g = self.inner.lock().unwrap();
        Ok(g.approvals.get(id).filter(|a| a.tenant == tenant).cloned())
    }

    async fn recover_interrupted(&self, _source_prefix: &str) -> Result<u64> {
        // Nothing survives a restart in memory.
        Ok(0)
    }

    async fn tenant_limits(&self, tenant: &str) -> Result<Option<TenantLimits>> {
        Ok(self
            .inner
            .lock()
            .unwrap()
            .tenant_limits
            .get(tenant)
            .cloned())
    }

    async fn set_tenant_limits(
        &self,
        tenant: &str,
        limits: &TenantLimits,
        _by: &str,
    ) -> Result<()> {
        self.inner
            .lock()
            .unwrap()
            .tenant_limits
            .insert(tenant.to_string(), limits.clone());
        Ok(())
    }

    async fn list_approvals(
        &self,
        tenant: &str,
        status: Option<ApprovalStatus>,
    ) -> Result<Vec<Approval>> {
        let g = self.inner.lock().unwrap();
        Ok(g.approval_order
            .iter()
            .rev()
            .filter_map(|id| g.approvals.get(id))
            .filter(|a| a.tenant == tenant && status.is_none_or(|s| a.status == s))
            // Same bound as PgStore (LIMIT 200): the API does not page.
            .take(200)
            .cloned()
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approval(id: &str) -> Approval {
        Approval {
            id: id.to_string(),
            tenant: "t".into(),
            run_id: "r".into(),
            tool: "read_file".into(),
            arguments: serde_json::json!({}),
            reason: "test".into(),
            status: ApprovalStatus::Pending,
            decided_by: None,
            decision_note: None,
            created_at_ms: 0,
            decided_at_ms: None,
        }
    }

    #[tokio::test]
    async fn list_approvals_is_capped_like_postgres() {
        let store = MemoryStore::new();
        for n in 0..205 {
            store
                .create_approval(&approval(&format!("a-{n:03}")))
                .await
                .unwrap();
        }
        let list = store.list_approvals("t", None).await.unwrap();
        assert_eq!(list.len(), 200);
    }
}
