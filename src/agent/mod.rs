use anyhow::{bail, Result};
use serde_json::Value;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tracing::{debug, info, warn};

use crate::approvals::{ApprovalRequest, Approver, DenyAll, Outcome};
use crate::audit::{Audit, Recorder, RunInfo};
use crate::config::{LimitsConfig, Pricing, ProfileConfig};
use crate::llm::{strip_think_blocks, ChatResponse, LlmClient, Message, ToolCall, ToolDefinition};
use crate::policy::{Decision, Policy};
use crate::store::{LlmCallRecord, RunEnd, ToolCallRecord};
use crate::tools::ToolRegistry;
use crate::util::truncate_utf8;

/// A limit outside the run (e.g. a tenant's monthly budget), checked
/// before each model call. What `admit` returns is held until the call is
/// recorded, so calls under way count against the limit.
#[async_trait::async_trait]
pub trait BudgetGuard: Send + Sync {
    async fn admit(&self) -> Result<Box<dyn std::any::Any + Send>>;
}

/// One ReAct loop: the model proposes, the policy decides, the tool runs
/// and the recorder writes it down.
pub struct Agent<'a> {
    llm: &'a dyn LlmClient,
    tools: &'a ToolRegistry,
    max_iterations: usize,
    max_run: Duration,
    max_tool_output_bytes: usize,
    max_tokens: Option<u64>,
    max_cost: Option<f64>,
    pricing: Option<Pricing>,
    allowed_tools: Vec<String>,
    policy: Arc<Policy>,
    approver: Arc<dyn Approver>,
    recorder: Option<Arc<dyn Recorder>>,
    budget: Option<Arc<dyn BudgetGuard>>,
    tenant: String,
    profile: String,
    source: String,
}

/// Running totals for one run.
#[derive(Default)]
struct RunState {
    iterations: usize,
    input_tokens: u64,
    output_tokens: u64,
    cost: Option<f64>,
    tainted: bool,
    /// The run has read the operator's own data (see `Policy::reads_private`).
    read_private: bool,
}

impl<'a> Agent<'a> {
    pub fn new(llm: &'a dyn LlmClient, tools: &'a ToolRegistry, max_iterations: usize) -> Self {
        let limits = LimitsConfig::default();
        Self {
            llm,
            tools,
            max_iterations,
            max_run: Duration::from_secs(limits.max_run_seconds),
            max_tool_output_bytes: limits.max_tool_output_bytes,
            max_tokens: None,
            max_cost: None,
            pricing: None,
            allowed_tools: Vec::new(),
            policy: Arc::new(Policy::default()),
            approver: Arc::new(DenyAll),
            recorder: None,
            budget: None,
            tenant: "default".into(),
            profile: "default".into(),
            source: "cli".into(),
        }
    }

    pub fn with_limits(mut self, limits: &LimitsConfig) -> Self {
        self.max_run = Duration::from_secs(limits.max_run_seconds.max(1));
        self.max_tool_output_bytes = limits.max_tool_output_bytes.max(256);
        self
    }

    /// Apply a profile: tool subset, budgets and limits it sets.
    pub fn with_profile(mut self, name: &str, p: &ProfileConfig) -> Self {
        self.profile = name.to_string();
        self.allowed_tools = p.tools.clone();
        if let Some(n) = p.max_iterations {
            self.max_iterations = n.max(1);
        }
        if let Some(s) = p.max_run_seconds {
            self.max_run = Duration::from_secs(s.max(1));
        }
        self.max_tokens = p.max_tokens;
        self.max_cost = p.max_cost;
        self
    }

    pub fn with_pricing(mut self, pricing: Option<Pricing>) -> Self {
        self.pricing = pricing;
        self
    }

    pub fn with_policy(mut self, policy: Arc<Policy>) -> Self {
        self.policy = policy;
        self
    }

    pub fn with_approver(mut self, approver: Arc<dyn Approver>) -> Self {
        self.approver = approver;
        self
    }

    pub fn with_recorder(mut self, recorder: Option<Arc<dyn Recorder>>) -> Self {
        self.recorder = recorder;
        self
    }

    /// Shorthand for a JSONL audit file as the only recorder.
    pub fn with_audit(mut self, audit: Option<Arc<Audit>>, source: &str) -> Self {
        self.recorder = audit.map(|a| a as Arc<dyn Recorder>);
        self.source = source.to_string();
        self
    }

    pub fn with_budget(mut self, budget: Option<Arc<dyn BudgetGuard>>) -> Self {
        self.budget = budget;
        self
    }

    pub fn with_identity(mut self, tenant: &str, source: &str) -> Self {
        self.tenant = tenant.to_string();
        self.source = source.to_string();
        self
    }

    pub async fn run(
        &self,
        system_prompt: &str,
        task: &str,
        progress: impl Fn(&str),
    ) -> Result<String> {
        let id = uuid::Uuid::new_v4().to_string();
        self.run_with_id(&id, system_prompt, task, progress).await
    }

    /// Run with an id chosen by the caller (the API creates the run row
    /// first, so clients can poll it).
    pub async fn run_with_id(
        &self,
        run_id: &str,
        system_prompt: &str,
        task: &str,
        progress: impl Fn(&str),
    ) -> Result<String> {
        let info = RunInfo {
            id: run_id.to_string(),
            tenant: self.tenant.clone(),
            profile: self.profile.clone(),
            source: self.source.clone(),
            provider: self.llm.provider_name().to_string(),
            model: self.llm.model().to_string(),
        };
        if let Some(r) = &self.recorder {
            r.run_start(&info).await;
        }
        let deadline = Instant::now() + self.max_run;
        let mut state = RunState::default();
        let result = self
            .run_loop(&info, system_prompt, task, deadline, &mut state, &progress)
            .await;
        if let Some(r) = &self.recorder {
            let end = RunEnd {
                iterations: state.iterations,
                input_tokens: state.input_tokens,
                output_tokens: state.output_tokens,
                cost: state.cost,
                tainted: state.tainted,
                output: result.as_ref().ok().cloned(),
                error: result.as_ref().err().map(|e| e.to_string()),
            };
            r.run_end(&info, &end).await;
        }
        result
    }

    fn tool_definitions(&self) -> Vec<ToolDefinition> {
        self.tools
            .definitions()
            .into_iter()
            .filter(|d| self.tool_allowed(&d.name))
            .collect()
    }

    fn tool_allowed(&self, name: &str) -> bool {
        self.allowed_tools.is_empty() || self.allowed_tools.iter().any(|t| t == name)
    }

    fn account(&self, state: &mut RunState, response: &ChatResponse) -> Option<f64> {
        let u = response.usage?;
        state.input_tokens += u.input_tokens;
        state.output_tokens += u.output_tokens;
        let cost = response.cost(self.pricing)?;
        state.cost = Some(state.cost.unwrap_or(0.0) + cost);
        Some(cost)
    }

    fn check_budget(&self, state: &RunState) -> Result<()> {
        if let Some(max) = self.max_tokens {
            let used = state.input_tokens + state.output_tokens;
            if used > max {
                bail!("token budget exceeded ({used} of {max})");
            }
        }
        if let (Some(max), Some(cost)) = (self.max_cost, state.cost) {
            if cost > max {
                bail!("cost budget exceeded ({cost:.4} of {max})");
            }
        }
        Ok(())
    }

    async fn run_loop(
        &self,
        info: &RunInfo,
        system_prompt: &str,
        task: &str,
        deadline: Instant,
        state: &mut RunState,
        progress: &impl Fn(&str),
    ) -> Result<String> {
        let tool_defs = self.tool_definitions();
        let mut messages = vec![Message::system(system_prompt), Message::user(task)];

        for iteration in 0..self.max_iterations {
            state.iterations = iteration + 1;
            debug!("iteration {}", iteration + 1);

            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                bail!("run time limit reached ({}s)", self.max_run.as_secs());
            }
            let admitted = match &self.budget {
                Some(b) => Some(b.admit().await?),
                None => None,
            };
            let started = Instant::now();
            let response =
                match tokio::time::timeout(remaining, self.llm.chat(&messages, &tool_defs)).await {
                    Ok(r) => r,
                    Err(_) => Err(anyhow::anyhow!(
                        "run time limit reached ({}s)",
                        self.max_run.as_secs()
                    )),
                };
            let usage = response.as_ref().ok().and_then(|r| r.usage);
            let cost = response.as_ref().ok().and_then(|r| self.account(state, r));
            // A fallback that answered is recorded under its own name.
            let served = response.as_ref().ok().and_then(|r| r.served_by.as_ref());
            if let Some(r) = &self.recorder {
                r.llm_call(
                    info,
                    &LlmCallRecord {
                        provider: served
                            .map_or_else(|| info.provider.clone(), |s| s.provider.clone()),
                        model: served.map_or_else(|| info.model.clone(), |s| s.model.clone()),
                        duration_ms: started.elapsed().as_millis() as u64,
                        input_tokens: usage.map(|u| u.input_tokens),
                        output_tokens: usage.map(|u| u.output_tokens),
                        cost,
                        error: response.as_ref().err().map(|e| e.to_string()),
                    },
                )
                .await;
            }
            drop(admitted);
            let response = response?;
            self.check_budget(state)?;

            if response.tool_calls.is_empty() {
                info!("done after {} iterations", iteration + 1);
                return Ok(strip_think_blocks(&response.content));
            }

            messages.push(Message::assistant_with_tools(
                strip_think_blocks(&response.content),
                response.tool_calls.clone(),
            ));

            for tc in &response.tool_calls {
                let label = format!("tool:{}", tc.name);
                progress(&label);
                info!("{label}");
                let result_text = self.handle_tool_call(info, tc, deadline, state).await;
                messages.push(Message::tool_result(&tc.id, result_text));
            }
        }

        bail!("exceeded max_iterations ({})", self.max_iterations)
    }

    /// Policy check, optional approval, execution and recording of one
    /// call. Returns what the model sees.
    async fn handle_tool_call(
        &self,
        info: &RunInfo,
        tc: &ToolCall,
        deadline: Instant,
        state: &mut RunState,
    ) -> String {
        let started = Instant::now();
        let mut record = ToolCallRecord {
            tool: tc.name.clone(),
            argument_keys: tc
                .arguments
                .as_object()
                .map(|o| o.keys().cloned().collect())
                .unwrap_or_default(),
            decision: "allow".into(),
            decision_reason: None,
            approval_id: None,
            duration_ms: 0,
            output_bytes: None,
            error: None,
        };

        let decision = if self.tool_allowed(&tc.name) {
            self.policy
                .evaluate(&tc.name, &self.profile, state.tainted, state.read_private)
        } else {
            Decision::Deny(format!("tool not available in profile '{}'", self.profile))
        };
        record.decision = decision.label().into();
        record.decision_reason = decision.reason().map(str::to_string);

        let gate: Result<(), String> = match &decision {
            Decision::Allow => Ok(()),
            Decision::Deny(reason) => Err(format!("denied by policy: {reason}")),
            Decision::RequireApproval(reason) => {
                let req = ApprovalRequest {
                    tenant: self.tenant.clone(),
                    run_id: info.id.clone(),
                    tool: tc.name.clone(),
                    arguments: tc.arguments.clone(),
                    reason: reason.clone(),
                    run_deadline: Some(deadline),
                };
                match self.approver.request(req).await {
                    Ok((id, outcome)) => {
                        record.approval_id = id;
                        match outcome {
                            Outcome::Approved { by } => {
                                record.decision_reason =
                                    Some(format!("{reason}; approved by {by}"));
                                Ok(())
                            }
                            Outcome::Denied { by, note } => Err(format!(
                                "a person ({by}) refused this action{}",
                                note.map(|n| format!(": {n}")).unwrap_or_default()
                            )),
                            Outcome::Expired => {
                                Err("nobody approved this action in time".to_string())
                            }
                        }
                    }
                    Err(e) => Err(format!("approval could not be requested: {e}")),
                }
            }
        };

        let result = match gate {
            Err(msg) => Err(anyhow::anyhow!(msg)),
            Ok(()) => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                let r = match tokio::time::timeout(remaining, self.execute_tool(tc)).await {
                    Ok(r) => r,
                    Err(_) => Err(anyhow::anyhow!("stopped: run time limit reached")),
                };
                // Whatever the tool returned may carry instructions written
                // by a third party.
                if self.policy.taints(&tc.name) {
                    state.tainted = true;
                }
                if self.policy.reads_private(&tc.name) {
                    state.read_private = true;
                }
                r
            }
        };

        let text = match &result {
            Ok(out) => {
                record.output_bytes = Some(out.len() as u64);
                self.cap_output(out)
            }
            Err(e) => {
                warn!("tool {} error: {e}", tc.name);
                record.error = Some(e.to_string());
                format!("Error: {e}")
            }
        };
        record.duration_ms = started.elapsed().as_millis() as u64;
        if let Some(r) = &self.recorder {
            r.tool_call(info, &record).await;
        }
        text
    }

    async fn execute_tool(&self, tc: &ToolCall) -> Result<String> {
        match &tc.arguments {
            Value::Object(_) => self.tools.execute(&tc.name, &tc.arguments).await,
            // A model that sends malformed arguments is told so instead of
            // having the tool run with defaults.
            Value::String(raw) => bail!(
                "arguments for '{}' are not valid JSON: {}",
                tc.name,
                truncate_utf8(raw, 200)
            ),
            Value::Null => {
                self.tools
                    .execute(&tc.name, &Value::Object(Default::default()))
                    .await
            }
            _ => bail!("arguments for '{}' must be a JSON object", tc.name),
        }
    }

    fn cap_output(&self, out: &str) -> String {
        if out.len() <= self.max_tool_output_bytes {
            return out.to_string();
        }
        let shown = truncate_utf8(out, self.max_tool_output_bytes);
        format!(
            "{shown}\n[tool output truncated: {} of {} bytes shown]",
            shown.len(),
            out.len()
        )
    }
}
