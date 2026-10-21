//! HTTP API of the Core (`eullm-agent api`). The contract is
//! `docs/openapi.json`; tests check that both stay in step.

use anyhow::{bail, Context, Result};
use axum::extract::{FromRequestParts, Path, Query, State};
use axum::http::request::Parts;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::sync::Arc;

use crate::config::{ApiTokenConfig, TenantLimits};
use crate::llm::{Message, Role, ToolDefinition};
use crate::service::{Core, FetchError, LimitReached, StartRun};
use crate::store::ApprovalStatus;

pub const OPENAPI: &str = include_str!("../docs/openapi.json");

/// Every route, as (method, path) in OpenAPI notation; checked against the
/// spec by the contract tests.
pub const ROUTES: &[(&str, &str)] = &[
    ("get", "/v1/health"),
    ("get", "/v1/openapi.json"),
    ("post", "/v1/runs"),
    ("get", "/v1/runs"),
    ("get", "/v1/runs/{id}"),
    ("post", "/v1/llm/chat"),
    ("get", "/v1/approvals"),
    ("post", "/v1/approvals/{id}"),
    ("post", "/v1/fetch"),
    ("get", "/v1/usage"),
    ("get", "/v1/profiles"),
    ("put", "/v1/tenants/{name}/limits"),
];

/// Header with which a delegating token acts for one of its sub-tenants.
pub const TENANT_HEADER: &str = "x-eullm-tenant";

const MAX_INPUT_BYTES: usize = 100 * 1024;

#[derive(Clone)]
pub struct ApiToken {
    pub name: String,
    pub tenant: String,
    pub profiles: Vec<String>,
    /// May act for `<tenant>/<name>` with the `X-EuLLM-Tenant` header.
    pub delegate: bool,
    sha256: [u8; 32],
}

pub fn sha256_hex(s: &str) -> String {
    Sha256::digest(s.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn from_hex(h: &str) -> Option<[u8; 32]> {
    let mut out = [0u8; 32];
    if h.len() != 64 {
        return None;
    }
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&h[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

impl ApiToken {
    pub fn resolve(cfg: &ApiTokenConfig) -> Result<Self> {
        let sha256 = match (&cfg.token_sha256, &cfg.token_env) {
            (Some(h), _) => from_hex(&h.to_ascii_lowercase())
                .with_context(|| format!("api token '{}': bad token_sha256", cfg.name))?,
            (None, Some(var)) => {
                let t = std::env::var(var)
                    .with_context(|| format!("environment variable {var} is not set"))?;
                // Files and shells often add a trailing newline; the request
                // side trims the Bearer token, so trim here too.
                let t = t.trim().to_string();
                if t.len() < 32 {
                    bail!("api token '{}' is shorter than 32 characters", cfg.name);
                }
                Sha256::digest(t.as_bytes()).into()
            }
            (None, None) => bail!("api token '{}' has no secret", cfg.name),
        };
        Ok(Self {
            name: cfg.name.clone(),
            tenant: cfg.tenant.clone(),
            profiles: cfg.profiles.clone(),
            delegate: cfg.delegate,
            sha256,
        })
    }

    fn may_use_profile(&self, profile: &str) -> bool {
        self.profiles.is_empty() || self.profiles.iter().any(|p| p == profile)
    }
}

/// A new random token (two v4 UUIDs, 244 random bits).
pub fn new_token() -> String {
    format!(
        "eat_{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

#[derive(Clone)]
pub struct AppState {
    pub core: Arc<Core>,
    pub tokens: Arc<Vec<ApiToken>>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/openapi.json", get(openapi))
        .route("/v1/runs", post(create_run).get(list_runs))
        .route("/v1/runs/{id}", get(get_run))
        .route("/v1/llm/chat", post(chat))
        .route("/v1/approvals", get(list_approvals))
        .route("/v1/approvals/{id}", post(decide_approval))
        .route("/v1/fetch", post(fetch))
        .route("/v1/usage", get(usage))
        .route("/v1/profiles", get(profiles))
        .route("/v1/tenants/{name}/limits", put(set_tenant_limits))
        .with_state(state)
        .merge(crate::web::router())
}

pub struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "error": self.1 }))).into_response()
    }
}

impl From<anyhow::Error> for ApiError {
    // A tenant at its limit gets 429 with the reason; anything else is
    // logged and hidden.
    fn from(e: anyhow::Error) -> Self {
        if let Some(l) = e.downcast_ref::<LimitReached>() {
            return ApiError(StatusCode::TOO_MANY_REQUESTS, l.0.clone());
        }
        tracing::warn!("api error: {e:#}");
        ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal error".into())
    }
}

fn bad_request(msg: impl Into<String>) -> ApiError {
    ApiError(StatusCode::BAD_REQUEST, msg.into())
}

/// The authenticated caller.
pub struct Caller(ApiToken);

impl FromRequestParts<AppState> for Caller {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, ApiError> {
        let unauthorized = || ApiError(StatusCode::UNAUTHORIZED, "missing or invalid token".into());
        let token = parts
            .headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .ok_or_else(unauthorized)?;
        let digest: [u8; 32] = Sha256::digest(token.trim().as_bytes()).into();
        // Compare every byte of every token so timing reveals nothing.
        let mut found = None;
        for t in state.tokens.iter() {
            let diff = t
                .sha256
                .iter()
                .zip(digest.iter())
                .fold(0u8, |acc, (a, b)| acc | (a ^ b));
            if diff == 0 {
                found = Some(t.clone());
            }
        }
        let mut caller = found.ok_or_else(unauthorized)?;
        if let Some(sub) = parts.headers.get(TENANT_HEADER) {
            let sub = sub.to_str().unwrap_or_default();
            if !caller.delegate {
                return Err(ApiError(
                    StatusCode::FORBIDDEN,
                    "this token cannot act for another tenant".into(),
                ));
            }
            if !valid_sub_tenant(sub) {
                return Err(bad_request(
                    "X-EuLLM-Tenant: 1 to 64 characters, a-z 0-9 - _ . and not starting with a symbol",
                ));
            }
            caller.tenant = format!("{}/{sub}", caller.tenant);
            // A sub-tenant cannot delegate further.
            caller.delegate = false;
        }
        Ok(Caller(caller))
    }
}

/// A sub-tenant name: lower-case letters, digits, `-`, `_`, `.`, starting
/// with a letter or a digit, so `parent/name` stays one unambiguous level.
fn valid_sub_tenant(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
        && s.chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || "-_.".contains(c))
}

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok", "version": env!("CARGO_PKG_VERSION") }))
}

async fn openapi() -> Response {
    ([(header::CONTENT_TYPE, "application/json")], OPENAPI).into_response()
}

#[derive(Deserialize)]
struct CreateRun {
    #[serde(default = "default_profile")]
    profile: String,
    input: String,
}

fn default_profile() -> String {
    "default".into()
}

async fn create_run(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Json(body): Json<CreateRun>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    if body.input.trim().is_empty() || body.input.len() > MAX_INPUT_BYTES {
        return Err(bad_request(format!(
            "input must be between 1 and {MAX_INPUT_BYTES} bytes"
        )));
    }
    if !caller.may_use_profile(&body.profile) {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            format!("profile '{}' is not allowed for this token", body.profile),
        ));
    }
    if st.core.config.profile(&body.profile).is_none() {
        return Err(bad_request(format!("unknown profile '{}'", body.profile)));
    }
    let id = st
        .core
        .start_run(StartRun {
            tenant: caller.tenant.clone(),
            profile: body.profile,
            input: body.input,
            source: format!("api:{}", caller.name),
            notify: None,
        })
        .await?;
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({ "id": id, "status": "queued" })),
    ))
}

#[derive(Deserialize)]
struct ListQuery {
    limit: Option<u32>,
}

async fn list_runs(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Query(q): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    let runs = st
        .core
        .store
        .list_runs(&caller.tenant, q.limit.unwrap_or(50).clamp(1, 500))
        .await?;
    Ok(Json(json!({ "runs": runs })))
}

async fn get_run(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match st.core.store.get_run(&caller.tenant, &id).await? {
        Some(run) => Ok(Json(
            serde_json::to_value(run).map_err(anyhow::Error::from)?,
        )),
        None => Err(ApiError(StatusCode::NOT_FOUND, "run not found".into())),
    }
}

#[derive(Deserialize)]
struct ChatMessage {
    role: String,
    content: String,
}

#[derive(Deserialize)]
struct ChatTool {
    name: String,
    #[serde(default)]
    description: String,
    #[serde(default = "empty_object")]
    parameters: Value,
}

fn empty_object() -> Value {
    json!({ "type": "object", "properties": {} })
}

#[derive(Deserialize)]
struct ChatRequest {
    #[serde(default = "default_profile")]
    model: String,
    messages: Vec<ChatMessage>,
    #[serde(default)]
    tools: Vec<ChatTool>,
}

async fn chat(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Json(body): Json<ChatRequest>,
) -> Result<Json<Value>, ApiError> {
    if body.messages.is_empty() {
        return Err(bad_request("messages must not be empty"));
    }
    // A token limited to some profiles may use only their models.
    if !caller.profiles.is_empty() {
        let allowed = caller
            .profiles
            .iter()
            .filter_map(|p| st.core.config.profile(p))
            .any(|p| p.model == body.model);
        if !allowed {
            return Err(ApiError(
                StatusCode::FORBIDDEN,
                format!("model '{}' is not allowed for this token", body.model),
            ));
        }
    }
    if st.core.router.get(&body.model).is_none() {
        return Err(bad_request(format!("unknown model '{}'", body.model)));
    }
    let mut messages = Vec::with_capacity(body.messages.len());
    for m in body.messages {
        let role = match m.role.as_str() {
            "system" => Role::System,
            "user" => Role::User,
            "assistant" => Role::Assistant,
            other => return Err(bad_request(format!("unsupported role '{other}'"))),
        };
        messages.push(Message {
            role,
            content: m.content,
            tool_call_id: None,
            tool_calls: None,
        });
    }
    let tools: Vec<ToolDefinition> = body
        .tools
        .into_iter()
        .map(|t| ToolDefinition {
            name: t.name,
            description: t.description,
            parameters: t.parameters,
        })
        .collect();
    let (resp, cost) = st
        .core
        .chat(&caller.tenant, &body.model, &messages, &tools)
        .await
        .map_err(|e| {
            if e.is::<LimitReached>() {
                ApiError::from(e)
            } else {
                ApiError(StatusCode::BAD_GATEWAY, format!("model call failed: {e}"))
            }
        })?;
    // The model that answered: the fallback when the first one failed.
    let served_model = match &resp.served_by {
        Some(s) => s.model.clone(),
        None => st
            .core
            .router
            .get(&body.model)
            .map(|m| m.client.model().to_string())
            .unwrap_or_else(|| body.model.clone()),
    };
    Ok(Json(json!({
        "content": resp.content,
        "served_model": served_model,
        "tool_calls": resp.tool_calls.iter().map(|c| json!({
            "id": c.id, "name": c.name, "arguments": c.arguments,
        })).collect::<Vec<_>>(),
        "usage": resp.usage.map(|u| json!({
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        })),
        "cost": cost,
    })))
}

#[derive(Deserialize)]
struct FetchRequest {
    url: String,
    #[serde(default)]
    headers: std::collections::BTreeMap<String, String>,
}

async fn fetch(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Json(body): Json<FetchRequest>,
) -> Result<Json<Value>, ApiError> {
    use base64::Engine;
    let headers: Vec<(String, String)> = body.headers.into_iter().collect();
    match st.core.fetch(&caller.tenant, &body.url, &headers).await {
        Ok(f) => Ok(Json(json!({
            "url": f.url.as_str(),
            "status": f.status,
            "content_type": f.content_type,
            "etag": f.etag,
            "last_modified": f.last_modified,
            "body_base64": base64::engine::general_purpose::STANDARD.encode(&f.body),
            "truncated": f.truncated,
            "redirects": f.redirects,
        }))),
        Err(FetchError::Disabled) => Err(ApiError(
            StatusCode::NOT_FOUND,
            "fetch is not enabled (api.fetch)".into(),
        )),
        Err(FetchError::Refused(m)) => Err(ApiError(StatusCode::FORBIDDEN, m)),
        Err(FetchError::Failed(m)) => Err(ApiError(StatusCode::BAD_GATEWAY, m)),
        Err(FetchError::Limited(m)) => Err(ApiError(StatusCode::TOO_MANY_REQUESTS, m)),
    }
}

async fn usage(
    State(st): State<AppState>,
    Caller(caller): Caller,
) -> Result<Json<Value>, ApiError> {
    let u = st.core.usage(&caller.tenant).await?;
    Ok(Json(json!({
        "tenant": caller.tenant,
        "day": u.day,
        "month": u.month,
        "limits": u.limits,
    })))
}

/// Set the limits of a sub-tenant of the caller's tenant (`<tenant>/<name>`),
/// for an application that adds customers without touching the
/// configuration. Only a delegating token, for its own sub-tenants.
async fn set_tenant_limits(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Path(name): Path<String>,
    Json(limits): Json<TenantLimits>,
) -> Result<Json<Value>, ApiError> {
    if !caller.delegate {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "only a delegating token sets the limits of its sub-tenants".into(),
        ));
    }
    if !valid_sub_tenant(&name) {
        return Err(bad_request("invalid tenant name"));
    }
    let negative = [limits.max_cost_per_month]
        .into_iter()
        .flatten()
        .any(|v| !v.is_finite() || v < 0.0);
    if negative {
        return Err(bad_request("max_cost_per_month must be a number >= 0"));
    }
    let tenant = format!("{}/{name}", caller.tenant);
    st.core
        .set_limits(&tenant, &limits, &format!("api:{}", caller.name))
        .await?;
    let effective = st.core.limits(&tenant).await?.unwrap_or_default();
    Ok(Json(json!({
        "tenant": tenant,
        "limits": limits,
        "effective": effective,
    })))
}

/// The profiles this token may use, with the model each one calls.
async fn profiles(State(st): State<AppState>, Caller(caller): Caller) -> Json<Value> {
    let config = &st.core.config;
    let mut names: Vec<&String> = config.profiles.keys().collect();
    let default = "default".to_string();
    if !config.profiles.contains_key("default") {
        names.push(&default);
    }
    names.sort();
    let list: Vec<Value> = names
        .into_iter()
        .filter(|n| caller.may_use_profile(n))
        .filter_map(|n| {
            let p = config.profile(n)?;
            let client = st.core.router.get(&p.model).map(|m| &m.client);
            Some(json!({
                "name": n,
                "model": p.model,
                "provider": client.map(|c| c.provider_name()),
                "provider_model": client.map(|c| c.model()),
                "tools": p.tools,
            }))
        })
        .collect();
    Json(json!({ "profiles": list }))
}

#[derive(Deserialize)]
struct ApprovalQuery {
    status: Option<String>,
}

async fn list_approvals(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Query(q): Query<ApprovalQuery>,
) -> Result<Json<Value>, ApiError> {
    let status = match q.status.as_deref() {
        None | Some("all") => None,
        Some(s) => Some(
            ApprovalStatus::parse(s).ok_or_else(|| bad_request(format!("unknown status '{s}'")))?,
        ),
    };
    let list = st.core.store.list_approvals(&caller.tenant, status).await?;
    Ok(Json(json!({ "approvals": list })))
}

#[derive(Deserialize)]
struct Decide {
    decision: String,
    note: Option<String>,
}

async fn decide_approval(
    State(st): State<AppState>,
    Caller(caller): Caller,
    Path(id): Path<String>,
    Json(body): Json<Decide>,
) -> Result<Json<Value>, ApiError> {
    let approve = match body.decision.as_str() {
        "approve" => true,
        "deny" => false,
        _ => return Err(bad_request("decision must be 'approve' or 'deny'")),
    };
    let decided = st
        .core
        .approvals
        .decide(
            &caller.tenant,
            &id,
            approve,
            &format!("api:{}", caller.name),
            body.note.as_deref(),
        )
        .await?;
    match decided {
        Some(a) => Ok(Json(serde_json::to_value(a).map_err(anyhow::Error::from)?)),
        None => Err(ApiError(
            StatusCode::CONFLICT,
            "approval not found or already decided".into(),
        )),
    }
}

/// Run the API server until the process stops.
pub async fn serve(core: Arc<Core>, listen: &str, tokens: Vec<ApiToken>) -> Result<()> {
    if tokens.is_empty() {
        bail!("api.tokens is empty: refusing to start an API nobody can authenticate to");
    }
    let app = router(AppState {
        core,
        tokens: Arc::new(tokens),
    });
    let listener = tokio::net::TcpListener::bind(listen)
        .await
        .with_context(|| format!("cannot listen on {listen}"))?;
    tracing::info!("API listening on http://{listen}");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

/// Ctrl-C or SIGTERM: stop accepting requests and let the open ones finish.
async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {},
        _ = term => {},
    }
    tracing::info!("shutting down: no new requests accepted");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_from_env_ignores_surrounding_whitespace() {
        let var = "EULLM_AGENT_TEST_TOKEN_TRIM";
        std::env::set_var(var, "test-token-0123456789abcdef01234567\n");
        let token = ApiToken::resolve(&ApiTokenConfig {
            name: "t".into(),
            tenant: "default".into(),
            token_sha256: None,
            token_env: Some(var.into()),
            profiles: vec![],
            delegate: false,
            delegate_limits: None,
        })
        .unwrap();
        std::env::remove_var(var);
        let expected: [u8; 32] = Sha256::digest(b"test-token-0123456789abcdef01234567").into();
        assert_eq!(token.sha256, expected);
    }
}
