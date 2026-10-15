use anyhow::{bail, Result};
use async_trait::async_trait;
use reqwest::{Client, RequestBuilder, Response, StatusCode};
use serde_json::Value;
use std::time::Duration;

pub mod anthropic;
pub mod eullm;
pub mod ollama;
pub mod openai;

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Value,
}

#[derive(Debug, Clone)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}

#[derive(Debug, Clone)]
pub struct Message {
    pub role: Role,
    pub content: String,
    pub tool_call_id: Option<String>,
    pub tool_calls: Option<Vec<ToolCall>>,
}

impl Message {
    pub fn system(content: impl Into<String>) -> Self {
        Self {
            role: Role::System,
            content: content.into(),
            tool_call_id: None,
            tool_calls: None,
        }
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: Role::User,
            content: content.into(),
            tool_call_id: None,
            tool_calls: None,
        }
    }

    pub fn assistant_with_tools(content: impl Into<String>, tool_calls: Vec<ToolCall>) -> Self {
        Self {
            role: Role::Assistant,
            content: content.into(),
            tool_call_id: None,
            tool_calls: Some(tool_calls),
        }
    }

    pub fn tool_result(tool_call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: Role::Tool,
            content: content.into(),
            tool_call_id: Some(tool_call_id.into()),
            tool_calls: None,
        }
    }
}

/// Tokens reported by the provider for one call.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
}

#[derive(Debug, Clone)]
pub struct ChatResponse {
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub usage: Option<Usage>,
    /// Set by a fallback chain: the model that actually answered, so usage
    /// and cost are recorded against it, not against the first in the chain.
    pub served_by: Option<ServedBy>,
}

#[derive(Debug, Clone)]
pub struct ServedBy {
    pub provider: String,
    pub model: String,
    pub pricing: Option<crate::config::Pricing>,
}

impl ChatResponse {
    /// Cost of this answer at `pricing`, or at the price of the model that
    /// served it when a fallback answered.
    pub fn cost(&self, pricing: Option<crate::config::Pricing>) -> Option<f64> {
        let pricing = match &self.served_by {
            Some(s) => s.pricing,
            None => pricing,
        }?;
        let u = self.usage?;
        Some(
            (u.input_tokens as f64 * pricing.input_per_mtok
                + u.output_tokens as f64 * pricing.output_per_mtok)
                / 1_000_000.0,
        )
    }
}

#[async_trait]
pub trait LlmClient: Send + Sync {
    async fn chat(&self, messages: &[Message], tools: &[ToolDefinition]) -> Result<ChatResponse>;
    fn provider_name(&self) -> &str;
    fn model(&self) -> &str;
}

/// HTTP client for model providers, with a request timeout.
pub fn http_client(timeout: Duration) -> Client {
    Client::builder()
        .timeout(timeout)
        .connect_timeout(Duration::from_secs(10))
        .user_agent(concat!("eullm-agent/", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("failed to build HTTP client")
}

/// Longest wait honoured from a `Retry-After` header.
const MAX_RETRY_WAIT: Duration = Duration::from_secs(30);

/// Send a request, retrying once when the provider is busy (429, 502, 503,
/// 504), and turn any other non-success status into an error carrying the
/// start of the response body.
pub async fn send_with_retry(build: impl Fn() -> RequestBuilder, what: &str) -> Result<Response> {
    let mut attempt = 0;
    loop {
        let resp = build()
            .send()
            .await
            .map_err(|e| anyhow::anyhow!("{what} request failed: {e}"))?;
        let status = resp.status();
        if status.is_success() {
            return Ok(resp);
        }
        let retryable = matches!(
            status,
            StatusCode::TOO_MANY_REQUESTS
                | StatusCode::BAD_GATEWAY
                | StatusCode::SERVICE_UNAVAILABLE
                | StatusCode::GATEWAY_TIMEOUT
        );
        if retryable && attempt == 0 {
            let wait = resp
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.trim().parse::<u64>().ok())
                .map(Duration::from_secs)
                .unwrap_or(Duration::from_secs(2))
                .min(MAX_RETRY_WAIT);
            tracing::warn!("{what} returned {status}, retrying in {}s", wait.as_secs());
            tokio::time::sleep(wait).await;
            attempt += 1;
            continue;
        }
        let body = resp.text().await.unwrap_or_default();
        bail!(
            "{what} error {status}: {}",
            crate::util::truncate_utf8(body.trim(), 500)
        );
    }
}

/// Remove <think>...</think> blocks emitted by reasoning models (e.g. Qwen3, DeepSeek).
pub fn strip_think_blocks(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(start) = rest.find("<think>") {
        out.push_str(&rest[..start]);
        match rest.find("</think>") {
            Some(end) => rest = &rest[end + "</think>".len()..],
            None => break,
        }
    }
    out.push_str(rest);
    out.trim().to_string()
}
