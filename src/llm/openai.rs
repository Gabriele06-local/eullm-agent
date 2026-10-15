use anyhow::{Context, Result};
use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::time::Duration;
use tracing::debug;

use super::{
    http_client, send_with_retry, ChatResponse, LlmClient, Message, Role, ToolCall, ToolDefinition,
    Usage,
};

/// Client for the OpenAI Chat Completions API and compatible servers
/// (EuLLM Engine `/v1`, vLLM, llama.cpp server, ...).
pub struct OpenAiClient {
    client: Client,
    base_url: String,
    api_key: Option<String>,
    model: String,
    provider: &'static str,
    /// Extra top-level fields merged into every request body.
    extra: Map<String, Value>,
}

impl OpenAiClient {
    pub fn new(
        api_key: Option<String>,
        model: impl Into<String>,
        base_url: Option<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            client: http_client(timeout),
            api_key,
            model: model.into(),
            base_url: base_url
                .unwrap_or_else(|| "https://api.openai.com/v1".into())
                .trim_end_matches('/')
                .to_string(),
            provider: "openai",
            extra: Map::new(),
        }
    }

    /// Name reported in logs and audit records.
    pub fn with_provider_name(mut self, name: &'static str) -> Self {
        self.provider = name;
        self
    }

    pub fn with_extra(mut self, extra: Map<String, Value>) -> Self {
        self.extra = extra;
        self
    }
}

#[derive(Deserialize)]
struct OpenAiResponse {
    choices: Vec<OpenAiChoice>,
    #[serde(default)]
    usage: Option<OpenAiUsage>,
}

#[derive(Deserialize)]
struct OpenAiUsage {
    #[serde(default)]
    prompt_tokens: u64,
    #[serde(default)]
    completion_tokens: u64,
}

#[derive(Deserialize)]
struct OpenAiChoice {
    message: OpenAiMessage,
}

#[derive(Deserialize)]
struct OpenAiMessage {
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    tool_calls: Option<Vec<OpenAiToolCall>>,
}

#[derive(Deserialize)]
struct OpenAiToolCall {
    id: String,
    function: OpenAiFunction,
}

#[derive(Deserialize)]
struct OpenAiFunction {
    name: String,
    /// Normally a JSON string; some servers send an object.
    arguments: Value,
}

fn to_openai_message(msg: &Message) -> Value {
    match msg.role {
        Role::System => json!({ "role": "system", "content": msg.content }),
        Role::User => json!({ "role": "user", "content": msg.content }),
        Role::Assistant => {
            let tool_calls: Option<Vec<Value>> = msg.tool_calls.as_ref().map(|calls| {
                calls
                    .iter()
                    .map(|c| {
                        json!({
                            "id": c.id,
                            "type": "function",
                            "function": { "name": c.name, "arguments": c.arguments.to_string() },
                        })
                    })
                    .collect()
            });
            let mut m = json!({ "role": "assistant" });
            if !msg.content.is_empty() {
                m["content"] = json!(msg.content);
            }
            if let Some(tc) = tool_calls {
                m["tool_calls"] = json!(tc);
            }
            m
        }
        Role::Tool => json!({
            "role": "tool",
            "tool_call_id": msg.tool_call_id.as_deref().unwrap_or(""),
            "content": msg.content,
        }),
    }
}

/// Parse tool-call arguments. Invalid JSON is kept as a string so the agent
/// can tell the model, instead of silently running the tool with `{}`.
pub(crate) fn parse_arguments(raw: Value) -> Value {
    match raw {
        Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::String(s)),
        other => other,
    }
}

#[async_trait]
impl LlmClient for OpenAiClient {
    async fn chat(&self, messages: &[Message], tools: &[ToolDefinition]) -> Result<ChatResponse> {
        let openai_tools: Vec<Value> = tools
            .iter()
            .map(|t| {
                json!({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                })
            })
            .collect();

        let mut body = json!({
            "model": self.model,
            "messages": messages.iter().map(to_openai_message).collect::<Vec<_>>(),
        });
        if !openai_tools.is_empty() {
            body["tools"] = Value::Array(openai_tools);
        }
        for (k, v) in &self.extra {
            body[k] = v.clone();
        }

        let url = format!("{}/chat/completions", self.base_url);
        debug!("POST {url}");

        let resp: OpenAiResponse = send_with_retry(
            || {
                let req = self.client.post(&url).json(&body);
                match &self.api_key {
                    Some(key) => req.bearer_auth(key),
                    None => req,
                }
            },
            self.provider,
        )
        .await?
        .json()
        .await
        .with_context(|| format!("{} parse error", self.provider))?;

        let msg = resp
            .choices
            .into_iter()
            .next()
            .context("empty choices")?
            .message;
        let content = msg.content.unwrap_or_default();
        let tool_calls = msg
            .tool_calls
            .unwrap_or_default()
            .into_iter()
            .map(|tc| ToolCall {
                id: tc.id,
                name: tc.function.name,
                arguments: parse_arguments(tc.function.arguments),
            })
            .collect();

        Ok(ChatResponse {
            content,
            tool_calls,
            usage: resp.usage.map(|u| Usage {
                input_tokens: u.prompt_tokens,
                output_tokens: u.completion_tokens,
            }),
            served_by: None,
        })
    }

    fn provider_name(&self) -> &str {
        self.provider
    }

    fn model(&self) -> &str {
        &self.model
    }
}
