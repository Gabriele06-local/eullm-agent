//! Model Router: named models with fallback and pricing.
//!
//! `default` is the top-level `provider`; `models` adds more. Profiles pick
//! a model by name, so changing provider never touches the callers.

use anyhow::{bail, Context, Result};
use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::config::{Config, Pricing};
use crate::llm::{ChatResponse, LlmClient, Message, ServedBy, ToolCall, ToolDefinition, Usage};
use crate::setup::build_client;

#[derive(Clone)]
pub struct RoutedModel {
    pub client: Arc<dyn LlmClient>,
    pub pricing: Option<Pricing>,
}

/// Client, pricing and fallback name of a configured model.
type Entry = (Arc<dyn LlmClient>, Option<Pricing>, Option<String>);

pub struct ModelRouter {
    models: HashMap<String, RoutedModel>,
}

impl ModelRouter {
    pub fn from_config(config: &Config) -> Result<Self> {
        let timeout = Duration::from_secs(config.limits.llm_timeout_seconds);
        let mut base: HashMap<String, Entry> = HashMap::new();
        base.insert(
            "default".into(),
            (
                EstimatedUsage::wrap(build_client(&config.provider, timeout)?),
                config.pricing,
                None,
            ),
        );
        for (name, m) in &config.models {
            let client =
                build_client(&m.provider, timeout).with_context(|| format!("models.{name}"))?;
            base.insert(
                name.clone(),
                (EstimatedUsage::wrap(client), m.pricing, m.fallback.clone()),
            );
        }
        let mut models = HashMap::new();
        for name in base.keys() {
            // Follow the fallback chain, refusing cycles.
            let mut chain: Vec<(Arc<dyn LlmClient>, Option<Pricing>)> = Vec::new();
            let mut seen = vec![name.clone()];
            let mut cur = name.clone();
            loop {
                let (client, pricing, fallback) = &base[&cur];
                chain.push((Arc::clone(client), *pricing));
                match fallback {
                    Some(f) if seen.contains(f) => bail!("fallback cycle at model '{f}'"),
                    Some(f) => {
                        seen.push(f.clone());
                        cur = f.clone();
                    }
                    None => break,
                }
            }
            let client: Arc<dyn LlmClient> = if chain.len() == 1 {
                chain
                    .pop()
                    .expect("fallback chain always holds the model itself")
                    .0
            } else {
                Arc::new(FallbackClient { chain })
            };
            models.insert(
                name.clone(),
                RoutedModel {
                    client,
                    pricing: base[name].1,
                },
            );
        }
        Ok(Self { models })
    }

    /// For tests: one client under a name.
    pub fn single(name: &str, client: Arc<dyn LlmClient>) -> Self {
        let mut models = HashMap::new();
        models.insert(
            name.to_string(),
            RoutedModel {
                client,
                pricing: None,
            },
        );
        Self { models }
    }

    pub fn get(&self, name: &str) -> Option<&RoutedModel> {
        self.models.get(name)
    }

    /// Models without a price: their calls cost nothing to the budgets.
    pub fn unpriced(&self) -> Vec<&str> {
        let mut v: Vec<&str> = self
            .models
            .iter()
            .filter(|(_, m)| m.pricing.is_none())
            .map(|(n, _)| n.as_str())
            .collect();
        v.sort();
        v
    }

    pub fn names(&self) -> Vec<&str> {
        let mut v: Vec<&str> = self.models.keys().map(String::as_str).collect();
        v.sort();
        v
    }
}

/// Fills in the usage when a provider does not report it, so token and cost
/// budgets never count a call as free. The estimate is about four characters
/// per token over everything sent (messages and tool definitions) and
/// received: rough, but on the side of counting, not of missing.
pub struct EstimatedUsage {
    inner: Arc<dyn LlmClient>,
    warned: AtomicBool,
}

impl EstimatedUsage {
    pub fn wrap(inner: Arc<dyn LlmClient>) -> Arc<dyn LlmClient> {
        Arc::new(Self {
            inner,
            warned: AtomicBool::new(false),
        })
    }
}

fn estimate_tokens(chars: usize) -> u64 {
    (chars as u64).div_ceil(4).max(1)
}

fn call_chars(calls: &[ToolCall]) -> usize {
    calls
        .iter()
        .map(|c| c.name.len() + c.arguments.to_string().len())
        .sum()
}

pub fn estimate_usage(
    messages: &[Message],
    tools: &[ToolDefinition],
    response: &ChatResponse,
) -> Usage {
    let sent: usize = messages
        .iter()
        .map(|m| m.content.len() + m.tool_calls.as_deref().map_or(0, call_chars))
        .sum::<usize>()
        + tools
            .iter()
            .map(|t| t.name.len() + t.description.len() + t.parameters.to_string().len())
            .sum::<usize>();
    Usage {
        input_tokens: estimate_tokens(sent),
        output_tokens: estimate_tokens(response.content.len() + call_chars(&response.tool_calls)),
    }
}

#[async_trait]
impl LlmClient for EstimatedUsage {
    async fn chat(&self, messages: &[Message], tools: &[ToolDefinition]) -> Result<ChatResponse> {
        let mut response = self.inner.chat(messages, tools).await?;
        if response.usage.is_none() {
            if !self.warned.swap(true, Ordering::Relaxed) {
                tracing::warn!(
                    "{} / {} reports no token usage: budgets use an estimate",
                    self.inner.provider_name(),
                    self.inner.model()
                );
            }
            response.usage = Some(estimate_usage(messages, tools, &response));
        }
        Ok(response)
    }

    fn provider_name(&self) -> &str {
        self.inner.provider_name()
    }

    fn model(&self) -> &str {
        self.inner.model()
    }
}

/// Tries each model in order until one answers; the answer says which
/// model served it, with that model's price.
pub struct FallbackClient {
    chain: Vec<(Arc<dyn LlmClient>, Option<Pricing>)>,
}

impl FallbackClient {
    pub fn new(chain: Vec<(Arc<dyn LlmClient>, Option<Pricing>)>) -> Self {
        assert!(!chain.is_empty());
        Self { chain }
    }
}

#[async_trait]
impl LlmClient for FallbackClient {
    async fn chat(&self, messages: &[Message], tools: &[ToolDefinition]) -> Result<ChatResponse> {
        let mut last_err = None;
        for (i, (c, pricing)) in self.chain.iter().enumerate() {
            match c.chat(messages, tools).await {
                Ok(mut r) => {
                    r.served_by = Some(ServedBy {
                        provider: c.provider_name().to_string(),
                        model: c.model().to_string(),
                        pricing: *pricing,
                    });
                    return Ok(r);
                }
                Err(e) => {
                    if i + 1 < self.chain.len() {
                        tracing::warn!(
                            "{} / {} failed, trying fallback: {e}",
                            c.provider_name(),
                            c.model()
                        );
                    }
                    last_err = Some(e);
                }
            }
        }
        Err(last_err.expect("fallback chain is never empty"))
    }

    fn provider_name(&self) -> &str {
        self.chain[0].0.provider_name()
    }

    fn model(&self) -> &str {
        self.chain[0].0.model()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Silent(Option<Usage>);

    #[async_trait]
    impl LlmClient for Silent {
        async fn chat(&self, _: &[Message], _: &[ToolDefinition]) -> Result<ChatResponse> {
            Ok(ChatResponse {
                content: "a".repeat(400),
                tool_calls: vec![],
                usage: self.0,
                served_by: None,
            })
        }
        fn provider_name(&self) -> &str {
            "silent"
        }
        fn model(&self) -> &str {
            "m"
        }
    }

    #[tokio::test]
    async fn missing_usage_is_estimated_not_zero() {
        let client = EstimatedUsage::wrap(Arc::new(Silent(None)));
        let tools = [ToolDefinition {
            name: "t".into(),
            description: "d".repeat(39),
            parameters: json!({}),
        }];
        let r = client
            .chat(&[Message::user("x".repeat(800))], &tools)
            .await
            .unwrap();
        let u = r.usage.expect("usage is filled in");
        assert_eq!(u.output_tokens, 100);
        assert_eq!(u.input_tokens, 211);
    }

    #[tokio::test]
    async fn reported_usage_is_kept() {
        let reported = Usage {
            input_tokens: 7,
            output_tokens: 3,
        };
        let client = EstimatedUsage::wrap(Arc::new(Silent(Some(reported))));
        let r = client.chat(&[Message::user("x")], &[]).await.unwrap();
        assert_eq!(r.usage, Some(reported));
    }

    struct Failing;

    #[async_trait]
    impl LlmClient for Failing {
        async fn chat(&self, _: &[Message], _: &[ToolDefinition]) -> Result<ChatResponse> {
            bail!("down")
        }
        fn provider_name(&self) -> &str {
            "primary"
        }
        fn model(&self) -> &str {
            "big"
        }
    }

    #[tokio::test]
    async fn a_fallback_answer_is_billed_to_the_fallback() {
        let usage = Usage {
            input_tokens: 1_000_000,
            output_tokens: 0,
        };
        let price = |p| {
            Some(Pricing {
                input_per_mtok: p,
                output_per_mtok: 0.0,
            })
        };
        let chain = FallbackClient::new(vec![
            (Arc::new(Failing), price(10.0)),
            (Arc::new(Silent(Some(usage))), price(1.0)),
        ]);
        let r = chain.chat(&[Message::user("x")], &[]).await.unwrap();
        let served = r.served_by.as_ref().expect("the chain says who answered");
        assert_eq!(
            (served.provider.as_str(), served.model.as_str()),
            ("silent", "m")
        );
        // The primary's price (10) would be the wrong bill.
        assert_eq!(r.cost(price(10.0)), Some(1.0));
    }

    #[test]
    fn default_model_takes_the_top_level_price() {
        let config: Config = serde_yaml::from_str(
            "provider:\n  type: eullm\n  model: test\npricing: { input_per_mtok: 1.0, output_per_mtok: 2.0 }\n",
        )
        .unwrap();
        let router = ModelRouter::from_config(&config).unwrap();
        assert_eq!(
            router.get("default").unwrap().pricing,
            Some(Pricing {
                input_per_mtok: 1.0,
                output_per_mtok: 2.0
            })
        );
        assert!(router.unpriced().is_empty());
    }
}
