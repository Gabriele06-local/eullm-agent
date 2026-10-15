use anyhow::{Context, Result};
use async_trait::async_trait;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::time::Duration;
use tracing::debug;

use super::{
    http_client, send_with_retry, ChatResponse, LlmClient, Message, Role, ToolCall, ToolDefinition,
    Usage,
};

const DEFAULT_BASE_URL: &str = "https://api.anthropic.com/v1";

pub struct AnthropicClient {
    client: Client,
    base_url: String,
    api_key: String,
    model: String,
    max_tokens: u32,
}

impl AnthropicClient {
    pub fn new(
        api_key: impl Into<String>,
        model: impl Into<String>,
        max_tokens: u32,
        timeout: Duration,
    ) -> Self {
        Self {
            client: http_client(timeout),
            base_url: DEFAULT_BASE_URL.into(),
            api_key: api_key.into(),
            model: model.into(),
            max_tokens: max_tokens.max(1),
        }
    }

    /// Point the client at another endpoint (used by tests).
    pub fn with_base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = base_url.into().trim_end_matches('/').to_string();
        self
    }
}

#[derive(Serialize)]
struct AnthropicRequest {
    model: String,
    max_tokens: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    system: Option<String>,
    messages: Vec<AnthropicMessage>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    tools: Vec<Value>,
}

#[derive(Serialize, Deserialize)]
struct AnthropicMessage {
    role: String,
    content: Value,
}

#[derive(Deserialize)]
struct AnthropicResponse {
    content: Vec<AnthropicBlock>,
    #[serde(default)]
    usage: Option<AnthropicUsage>,
}

#[derive(Deserialize)]
struct AnthropicUsage {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum AnthropicBlock {
    Text {
        text: String,
    },
    ToolUse {
        id: String,
        name: String,
        input: Value,
    },
    /// Thinking, server tool results and future block types are ignored
    /// instead of failing the whole response.
    #[serde(other)]
    Unknown,
}

fn to_anthropic_messages(messages: &[Message]) -> (Option<String>, Vec<AnthropicMessage>) {
    let mut system = None;
    let mut out: Vec<AnthropicMessage> = Vec::new();

    for msg in messages {
        match msg.role {
            Role::System => {
                system = Some(msg.content.clone());
            }
            Role::User => {
                out.push(AnthropicMessage {
                    role: "user".into(),
                    content: Value::String(msg.content.clone()),
                });
            }
            Role::Assistant => {
                let mut parts: Vec<Value> = Vec::new();
                if !msg.content.is_empty() {
                    parts.push(json!({ "type": "text", "text": msg.content }));
                }
                if let Some(calls) = &msg.tool_calls {
                    for c in calls {
                        parts.push(json!({
                            "type": "tool_use",
                            "id": c.id,
                            "name": c.name,
                            "input": c.arguments,
                        }));
                    }
                }
                out.push(AnthropicMessage {
                    role: "assistant".into(),
                    content: if parts.len() == 1 && parts[0]["type"] == "text" {
                        parts[0]["text"].clone()
                    } else {
                        Value::Array(parts)
                    },
                });
            }
            Role::Tool => {
                let tool_use_id = msg.tool_call_id.as_deref().unwrap_or("unknown");
                let new_part = json!({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": msg.content,
                });

                let merged = out.last_mut().filter(|m| m.role == "user").and_then(|m| {
                    if let Value::Array(arr) = &mut m.content {
                        arr.push(new_part.clone());
                        Some(())
                    } else {
                        None
                    }
                });

                if merged.is_none() {
                    out.push(AnthropicMessage {
                        role: "user".into(),
                        content: json!([new_part]),
                    });
                }
            }
        }
    }

    (system, out)
}

#[async_trait]
impl LlmClient for AnthropicClient {
    async fn chat(&self, messages: &[Message], tools: &[ToolDefinition]) -> Result<ChatResponse> {
        let (system, anthropic_messages) = to_anthropic_messages(messages);

        let anthropic_tools: Vec<Value> = tools
            .iter()
            .map(|t| {
                json!({
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                })
            })
            .collect();

        let req = AnthropicRequest {
            model: self.model.clone(),
            max_tokens: self.max_tokens,
            system,
            messages: anthropic_messages,
            tools: anthropic_tools,
        };

        let url = format!("{}/messages", self.base_url);
        debug!("POST {url}");

        let resp: AnthropicResponse = send_with_retry(
            || {
                self.client
                    .post(&url)
                    .header("x-api-key", &self.api_key)
                    .header("anthropic-version", "2023-06-01")
                    .json(&req)
            },
            "anthropic",
        )
        .await?
        .json()
        .await
        .context("anthropic parse error")?;

        let mut content = String::new();
        let mut tool_calls = Vec::new();

        for block in resp.content {
            match block {
                AnthropicBlock::Text { text } => content.push_str(&text),
                AnthropicBlock::ToolUse { id, name, input } => {
                    tool_calls.push(ToolCall {
                        id,
                        name,
                        arguments: input,
                    });
                }
                AnthropicBlock::Unknown => {}
            }
        }

        Ok(ChatResponse {
            content,
            tool_calls,
            usage: resp.usage.map(|u| Usage {
                input_tokens: u.input_tokens,
                output_tokens: u.output_tokens,
            }),
            served_by: None,
        })
    }

    fn provider_name(&self) -> &str {
        "anthropic"
    }

    fn model(&self) -> &str {
        &self.model
    }
}
