//! Helpers shared by the integration tests.
#![allow(dead_code)]

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// A fresh, empty directory under the system temp dir.
pub fn tmp(name: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("eullm-test-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir_all(&p).unwrap();
    p
}

/// One request received by [`MockServer`].
#[derive(Debug, Clone)]
pub struct Recorded {
    pub method: String,
    pub path: String,
    pub headers: Vec<(String, String)>,
    pub body: String,
}

impl Recorded {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// Minimal HTTP/1.1 server on 127.0.0.1 that answers with `respond` and
/// records every request. Enough for provider and fetch_url tests without
/// pulling in a mock-server crate.
pub struct MockServer {
    pub port: u16,
    pub requests: Arc<Mutex<Vec<Recorded>>>,
}

impl MockServer {
    pub async fn start<F>(respond: F) -> Self
    where
        F: Fn(&Recorded, usize) -> String + Send + Sync + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let reqs = Arc::clone(&requests);
        let respond = Arc::new(respond);
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                let reqs = Arc::clone(&reqs);
                let respond = Arc::clone(&respond);
                tokio::spawn(async move {
                    let Some(req) = read_request(&mut sock).await else {
                        return;
                    };
                    let n = {
                        let mut r = reqs.lock().unwrap();
                        r.push(req.clone());
                        r.len() - 1
                    };
                    let resp = respond(&req, n);
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });
        Self { port, requests }
    }

    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    pub fn recorded(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }
}

async fn read_request(sock: &mut tokio::net::TcpStream) -> Option<Recorded> {
    let mut buf = Vec::new();
    let mut tmp = [0u8; 4096];
    let header_end = loop {
        let n = sock.read(&mut tmp).await.ok()?;
        if n == 0 {
            return None;
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break i + 4;
        }
    };
    let head = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = head.split("\r\n");
    let mut first = lines.next()?.split(' ');
    let method = first.next()?.to_string();
    let path = first.next()?.to_string();
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect();
    let len: usize = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse().ok())
        .unwrap_or(0);
    while buf.len() < header_end + len {
        let n = sock.read(&mut tmp).await.ok()?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&tmp[..n]);
    }
    let body = String::from_utf8_lossy(&buf[header_end..]).to_string();
    Some(Recorded {
        method,
        path,
        headers,
        body,
    })
}

pub fn http_response(status: &str, extra_headers: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n{extra_headers}\r\n{body}",
        body.len()
    )
}

// --- scripted model and tools ------------------------------------------------

use async_trait::async_trait;
use eullm_agent::llm::{ChatResponse, LlmClient, Message, ToolCall, ToolDefinition, Usage};
use eullm_agent::tools::Tool;
use serde_json::{json, Value};

/// Plays back scripted responses, then answers "done"; records requests.
pub struct Scripted {
    pub script: Mutex<Vec<ChatResponse>>,
    pub seen: Mutex<Vec<Vec<Message>>>,
    /// How long each answer takes, to make calls overlap.
    pub delay: Mutex<std::time::Duration>,
}

impl Scripted {
    pub fn new(mut script: Vec<ChatResponse>) -> Self {
        script.reverse();
        Self {
            script: Mutex::new(script),
            seen: Mutex::new(Vec::new()),
            delay: Mutex::new(std::time::Duration::ZERO),
        }
    }

    pub fn last_tool_result(&self) -> Option<String> {
        let seen = self.seen.lock().unwrap();
        seen.last()?
            .iter()
            .rev()
            .find(|m| m.role == eullm_agent::llm::Role::Tool)
            .map(|m| m.content.clone())
    }
}

#[async_trait]
impl LlmClient for Scripted {
    async fn chat(
        &self,
        messages: &[Message],
        _: &[ToolDefinition],
    ) -> anyhow::Result<ChatResponse> {
        self.seen.lock().unwrap().push(messages.to_vec());
        let delay = *self.delay.lock().unwrap();
        if !delay.is_zero() {
            tokio::time::sleep(delay).await;
        }
        let next = self.script.lock().unwrap().pop();
        Ok(next.unwrap_or(ChatResponse {
            content: "done".into(),
            tool_calls: vec![],
            usage: Some(Usage {
                input_tokens: 10,
                output_tokens: 2,
            }),
            served_by: None,
        }))
    }
    fn provider_name(&self) -> &str {
        "scripted"
    }
    fn model(&self) -> &str {
        "test"
    }
}

pub fn tool_call(name: &str, arguments: Value) -> ChatResponse {
    ChatResponse {
        content: String::new(),
        tool_calls: vec![ToolCall {
            id: format!("call-{name}"),
            name: name.into(),
            arguments,
        }],
        usage: Some(Usage {
            input_tokens: 100,
            output_tokens: 20,
        }),
        served_by: None,
    }
}

/// A tool that counts its executions.
pub struct CountingTool {
    pub name: String,
    pub output: String,
    pub runs: Arc<Mutex<u32>>,
}

impl CountingTool {
    pub fn new(name: &str, output: &str) -> (Arc<Self>, Arc<Mutex<u32>>) {
        let runs = Arc::new(Mutex::new(0));
        (
            Arc::new(Self {
                name: name.into(),
                output: output.into(),
                runs: Arc::clone(&runs),
            }),
            runs,
        )
    }
}

#[async_trait]
impl Tool for CountingTool {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: self.name.clone(),
            description: String::new(),
            parameters: json!({"type": "object"}),
        }
    }
    async fn execute(&self, _: &Value) -> anyhow::Result<String> {
        *self.runs.lock().unwrap() += 1;
        Ok(self.output.clone())
    }
}
