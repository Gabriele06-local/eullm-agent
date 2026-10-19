//! EuLLM Agent: autonomous agent runtime with multi-provider LLM support.
//!
//! The binary in `main.rs` is a thin CLI over this library; integration
//! tests in `tests/` use the same modules.

pub mod agent;
pub mod api;
pub mod approvals;
pub mod audit;
pub mod config;
pub mod editor_mode;
pub mod llm;
pub mod modules;
pub mod policy;
pub mod router;
pub mod server_init;
pub mod service;
pub mod setup;
pub mod store;
pub mod telegram;
pub mod tools;
pub mod util;
pub mod web;
pub mod wizard;
