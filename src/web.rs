//! Web interface of the Core, served by `eullm-agent api` and
//! `eullm-agent start`: chat with the agent, runs, approvals and usage.
//!
//! The pages carry no data and need no login: the browser keeps the API
//! token (from the login form or the link `start` prints) and reads
//! everything through `/v1`, like any other client.

use axum::http::{header, HeaderValue};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;

const INDEX: &str = include_str!("../web/index.html");
const APP_JS: &str = include_str!("../web/app.js");
const APP_CSS: &str = include_str!("../web/app.css");

/// Only this origin's own files: no inline script, nothing loaded from
/// elsewhere, no framing.
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; \
                   connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

pub fn router<S: Clone + Send + Sync + 'static>() -> Router<S> {
    Router::new()
        .route(
            "/",
            get(|| async { page(INDEX, "text/html; charset=utf-8") }),
        )
        .route(
            "/ui/app.js",
            get(|| async { page(APP_JS, "text/javascript; charset=utf-8") }),
        )
        .route(
            "/ui/app.css",
            get(|| async { page(APP_CSS, "text/css; charset=utf-8") }),
        )
}

fn page(body: &'static str, content_type: &'static str) -> Response {
    let mut resp = body.into_response();
    let h = resp.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    h.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CSP),
    );
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    h.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    resp
}
