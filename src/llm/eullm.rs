//! EuLLM Engine provider.
//!
//! The Engine serves an OpenAI-compatible API under `/v1`. Its Ollama-style
//! `/api/chat` endpoint ignores `tools`, so tool calling only works through
//! `/v1/chat/completions`.

use std::time::Duration;

use super::openai::OpenAiClient;

/// Build a client for an EuLLM Engine at `base_url` (with or without `/v1`).
pub fn client(
    base_url: &str,
    model: impl Into<String>,
    api_key: Option<String>,
    timeout: Duration,
) -> OpenAiClient {
    OpenAiClient::new(api_key, model, Some(v1_url(base_url)), timeout).with_provider_name("eullm")
}

/// `http://host:port` → `http://host:port/v1`; an existing `/v1` is kept.
pub fn v1_url(base_url: &str) -> String {
    let Ok(mut url) = reqwest::Url::parse(base_url) else {
        let base = base_url.trim_end_matches('/');
        return if base.ends_with("/v1") {
            base.to_string()
        } else {
            format!("{base}/v1")
        };
    };
    let path = url.path().trim_end_matches('/');
    if !path.ends_with("/v1") {
        url.set_path(&format!("{path}/v1"));
    }
    let out = url.to_string();
    match out.find(['?', '#']) {
        Some(i) => format!("{}{}", out[..i].trim_end_matches('/'), &out[i..]),
        None => out.trim_end_matches('/').to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn v1_suffix_added_once() {
        assert_eq!(
            v1_url("http://localhost:11434"),
            "http://localhost:11434/v1"
        );
        assert_eq!(
            v1_url("http://localhost:11434/"),
            "http://localhost:11434/v1"
        );
        assert_eq!(
            v1_url("http://localhost:11434/v1/"),
            "http://localhost:11434/v1"
        );
    }

    #[test]
    fn v1_suffix_keeps_query_and_fragment() {
        assert_eq!(v1_url("http://host/v1?k=1"), "http://host/v1?k=1");
        assert_eq!(v1_url("http://host?k=1"), "http://host/v1?k=1");
        assert_eq!(v1_url("http://host/v1#frag"), "http://host/v1#frag");
        assert_eq!(v1_url("http://host/api?k=1"), "http://host/api/v1?k=1");
    }
}
