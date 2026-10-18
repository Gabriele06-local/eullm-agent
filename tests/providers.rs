//! Provider wire-format tests against a local mock server.

mod common;

use common::{http_response, MockServer};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

use eullm_agent::llm::{
    anthropic::AnthropicClient, eullm, ollama::OllamaClient, openai::OpenAiClient, LlmClient,
    Message, ToolDefinition,
};

fn weather_tool() -> Vec<ToolDefinition> {
    vec![ToolDefinition {
        name: "get_weather".into(),
        description: "Weather for a city".into(),
        parameters: json!({
            "type": "object",
            "properties": { "city": { "type": "string" } },
            "required": ["city"]
        }),
    }]
}

const TOOL_CALL_RESPONSE: &str = r#"{
  "choices": [{ "message": {
    "role": "assistant", "content": null,
    "tool_calls": [{ "id": "call_1", "type": "function",
      "function": { "name": "get_weather", "arguments": "{\"city\":\"Roma\"}" } }]
  }}],
  "usage": { "prompt_tokens": 12, "completion_tokens": 7 }
}"#;

#[tokio::test]
async fn eullm_uses_v1_chat_completions_with_tools() {
    let srv = MockServer::start(|_, _| http_response("200 OK", "", TOOL_CALL_RESPONSE)).await;
    let client = eullm::client(
        &srv.url(),
        "qwen3:8b",
        Some("k-123".into()),
        Duration::from_secs(5),
    );
    let resp = client
        .chat(&[Message::user("weather in Rome?")], &weather_tool())
        .await
        .unwrap();

    assert_eq!(resp.tool_calls.len(), 1);
    assert_eq!(resp.tool_calls[0].name, "get_weather");
    assert_eq!(resp.tool_calls[0].arguments, json!({"city": "Roma"}));
    let usage = resp.usage.unwrap();
    assert_eq!((usage.input_tokens, usage.output_tokens), (12, 7));
    assert_eq!(client.provider_name(), "eullm");

    let reqs = srv.recorded();
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].method, "POST");
    assert_eq!(reqs[0].path, "/v1/chat/completions");
    assert_eq!(reqs[0].header("authorization"), Some("Bearer k-123"));
    let body: Value = serde_json::from_str(&reqs[0].body).unwrap();
    assert_eq!(body["model"], "qwen3:8b");
    assert_eq!(body["tools"][0]["function"]["name"], "get_weather");
}

#[tokio::test]
async fn eullm_without_key_sends_no_authorization() {
    let srv = MockServer::start(|_, _| {
        http_response(
            "200 OK",
            "",
            r#"{"choices":[{"message":{"content":"hi"}}]}"#,
        )
    })
    .await;
    let client = eullm::client(
        &format!("{}/v1/", srv.url()),
        "m",
        None,
        Duration::from_secs(5),
    );
    let resp = client.chat(&[Message::user("hi")], &[]).await.unwrap();
    assert_eq!(resp.content, "hi");
    let reqs = srv.recorded();
    assert_eq!(reqs[0].path, "/v1/chat/completions");
    assert!(reqs[0].header("authorization").is_none());
    let body: Value = serde_json::from_str(&reqs[0].body).unwrap();
    assert!(body.get("tools").is_none());
}

#[tokio::test]
async fn openai_max_tokens_from_the_config_is_sent() {
    let srv = MockServer::start(|_, _| {
        http_response(
            "200 OK",
            "",
            r#"{"choices":[{"message":{"content":"ok"}}]}"#,
        )
    })
    .await;
    let provider: eullm_agent::config::ProviderConfig = from_json(&format!(
        r#"{{"type":"openai","api_key":"k","model":"m","base_url":"{}/v1","max_tokens":16384}}"#,
        srv.url()
    ));
    let client = eullm_agent::setup::build_client(&provider, Duration::from_secs(5)).unwrap();
    client.chat(&[Message::user("hi")], &[]).await.unwrap();
    let body: Value = serde_json::from_str(&srv.recorded()[0].body).unwrap();
    assert_eq!(body["max_tokens"], 16384);

    // Without it nothing is sent and the service decides.
    let provider: eullm_agent::config::ProviderConfig = from_json(&format!(
        r#"{{"type":"openai","api_key":"k","model":"m","base_url":"{}/v1"}}"#,
        srv.url()
    ));
    let client = eullm_agent::setup::build_client(&provider, Duration::from_secs(5)).unwrap();
    client.chat(&[Message::user("hi")], &[]).await.unwrap();
    let body: Value = serde_json::from_str(&srv.recorded()[1].body).unwrap();
    assert!(body.get("max_tokens").is_none());
}

fn from_json<T: serde::de::DeserializeOwned>(json_text: &str) -> T {
    serde_json::from_str(json_text).unwrap()
}

#[tokio::test]
async fn busy_engine_is_retried_once_after_retry_after() {
    let srv = MockServer::start(|_, n| {
        if n == 0 {
            http_response(
                "503 Service Unavailable",
                "Retry-After: 1\r\n",
                r#"{"error":"busy"}"#,
            )
        } else {
            http_response(
                "200 OK",
                "",
                r#"{"choices":[{"message":{"content":"ok"}}]}"#,
            )
        }
    })
    .await;
    let client = eullm::client(&srv.url(), "m", None, Duration::from_secs(5));
    let started = Instant::now();
    let resp = client.chat(&[Message::user("hi")], &[]).await.unwrap();
    assert_eq!(resp.content, "ok");
    assert_eq!(srv.recorded().len(), 2);
    assert!(started.elapsed() >= Duration::from_millis(900));
}

#[tokio::test]
async fn errors_carry_status_and_body() {
    let srv = MockServer::start(|_, _| {
        http_response("401 Unauthorized", "", r#"{"error":"invalid api key"}"#)
    })
    .await;
    let client = eullm::client(&srv.url(), "m", Some("bad".into()), Duration::from_secs(5));
    let err = client
        .chat(&[Message::user("hi")], &[])
        .await
        .unwrap_err()
        .to_string();
    assert!(err.contains("401"), "{err}");
    assert!(err.contains("invalid api key"), "{err}");
    assert_eq!(srv.recorded().len(), 1, "a 401 must not be retried");
}

#[tokio::test]
async fn slow_provider_times_out() {
    // Accepts the connection but never answers.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((s, _)) = listener.accept().await {
            held.push(s);
        }
    });
    let client = OpenAiClient::new(
        None,
        "m",
        Some(format!("http://127.0.0.1:{port}/v1")),
        Duration::from_secs(1),
    );
    let started = Instant::now();
    assert!(client.chat(&[Message::user("hi")], &[]).await.is_err());
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[tokio::test]
async fn invalid_tool_arguments_are_kept_as_text() {
    let srv = MockServer::start(|_, _| {
        http_response(
            "200 OK",
            "",
            r#"{"choices":[{"message":{"tool_calls":[{"id":"c","function":{"name":"get_weather","arguments":"{city: Roma"}}]}}]}"#,
        )
    })
    .await;
    let client = OpenAiClient::new(None, "m", Some(srv.url()), Duration::from_secs(5));
    let resp = client
        .chat(&[Message::user("x")], &weather_tool())
        .await
        .unwrap();
    assert_eq!(resp.tool_calls[0].arguments, json!("{city: Roma"));
}

#[tokio::test]
async fn ollama_uses_native_api_chat() {
    let srv = MockServer::start(|_, _| {
        http_response(
            "200 OK",
            "",
            r#"{"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"get_weather","arguments":{"city":"Roma"}}}]},"prompt_eval_count":5,"eval_count":3}"#,
        )
    })
    .await;
    let client = OllamaClient::new(srv.url(), "llama3.1", Duration::from_secs(5));
    let resp = client
        .chat(&[Message::user("x")], &weather_tool())
        .await
        .unwrap();
    assert_eq!(resp.tool_calls[0].arguments, json!({"city": "Roma"}));
    assert_eq!(resp.usage.unwrap().output_tokens, 3);
    assert_eq!(client.provider_name(), "ollama");
    let reqs = srv.recorded();
    assert_eq!(reqs[0].path, "/api/chat");
    let body: Value = serde_json::from_str(&reqs[0].body).unwrap();
    assert_eq!(body["stream"], false);
    assert_eq!(body["tools"][0]["function"]["name"], "get_weather");
}

#[tokio::test]
async fn anthropic_tolerates_unknown_blocks_and_reads_usage() {
    let srv = MockServer::start(|_, _| {
        http_response(
            "200 OK",
            "",
            r#"{"content":[
                {"type":"thinking","thinking":"...","signature":"s"},
                {"type":"text","text":"Checking."},
                {"type":"tool_use","id":"tu_1","name":"get_weather","input":{"city":"Roma"}}
            ],"usage":{"input_tokens":20,"output_tokens":9}}"#,
        )
    })
    .await;
    let client = AnthropicClient::new("sk-test", "claude-x", 1024, Duration::from_secs(5))
        .with_base_url(format!("{}/v1", srv.url()));
    let resp = client
        .chat(&[Message::user("x")], &weather_tool())
        .await
        .unwrap();
    assert_eq!(resp.content, "Checking.");
    assert_eq!(resp.tool_calls[0].id, "tu_1");
    assert_eq!(resp.usage.unwrap().input_tokens, 20);
    let reqs = srv.recorded();
    assert_eq!(reqs[0].path, "/v1/messages");
    assert_eq!(reqs[0].header("x-api-key"), Some("sk-test"));
    let body: Value = serde_json::from_str(&reqs[0].body).unwrap();
    assert_eq!(body["max_tokens"], 1024);
    assert_eq!(body["tools"][0]["input_schema"]["required"][0], "city");
}
