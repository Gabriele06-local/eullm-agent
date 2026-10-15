//! Tool policy: for every tool call the model asks for, decide whether it
//! runs, is refused, or waits for a human.
//!
//! Rules are checked in order and the first match wins. On top of them, a
//! run that has read external content (a web page, a file, a document) is
//! "tainted": from then on, tools with side effects need approval, because
//! the content may have been written to steer the model. And a tainted run
//! that has also read the operator's own data (workspace files, documents)
//! cannot send anything out without approval either: steering plus private
//! data plus a way out is how prompt injection steals data.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Effect {
    Allow,
    Deny,
    RequireApproval,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    Allow,
    Deny(String),
    RequireApproval(String),
}

impl Decision {
    pub fn label(&self) -> &'static str {
        match self {
            Decision::Allow => "allow",
            Decision::Deny(_) => "deny",
            Decision::RequireApproval(_) => "require_approval",
        }
    }

    pub fn reason(&self) -> Option<&str> {
        match self {
            Decision::Allow => None,
            Decision::Deny(r) | Decision::RequireApproval(r) => Some(r),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Rule {
    /// Tool name; `*` matches any tool, `prefix*` any tool starting with it.
    pub tool: String,
    /// Profiles the rule applies to; empty means all.
    #[serde(default)]
    pub profiles: Vec<String>,
    /// Applies only to tainted runs when true, only to clean runs when
    /// false, to both when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tainted: Option<bool>,
    pub effect: Effect,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TaintConfig {
    /// Tools whose output is external content.
    #[serde(default = "default_sources")]
    pub sources: Vec<String>,
    /// Tools with side effects, gated once the run is tainted.
    #[serde(default = "default_effects")]
    pub effects: Vec<String>,
    /// Tools whose output is the operator's own data.
    #[serde(default = "default_private_sources")]
    pub private_sources: Vec<String>,
    /// Tools that can carry data out (in a URL, a header, a body). Gated
    /// once the run is tainted and has read private data.
    #[serde(default = "default_egress")]
    pub egress: Vec<String>,
    #[serde(default = "default_taint_effect")]
    pub on_effect: Effect,
}

impl Default for TaintConfig {
    fn default() -> Self {
        Self {
            sources: default_sources(),
            effects: default_effects(),
            private_sources: default_private_sources(),
            egress: default_egress(),
            on_effect: default_taint_effect(),
        }
    }
}

fn default_sources() -> Vec<String> {
    ["fetch_url", "read_file", "ocr_image", "pdf_to_text"]
        .map(String::from)
        .to_vec()
}
fn default_effects() -> Vec<String> {
    ["write_file", "run_program"].map(String::from).to_vec()
}
fn default_private_sources() -> Vec<String> {
    ["read_file", "list_dir", "ocr_image", "pdf_to_text"]
        .map(String::from)
        .to_vec()
}
fn default_egress() -> Vec<String> {
    ["fetch_url"].map(String::from).to_vec()
}
fn default_taint_effect() -> Effect {
    Effect::RequireApproval
}

/// The policy file. Without one, every registered tool is allowed (the
/// configuration already decides which tools exist) and the taint rule
/// applies.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Policy {
    #[serde(default = "default_effect")]
    pub default: Effect,
    #[serde(default)]
    pub rules: Vec<Rule>,
    #[serde(default)]
    pub taint: TaintConfig,
}

fn default_effect() -> Effect {
    Effect::Allow
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            default: default_effect(),
            rules: Vec::new(),
            taint: TaintConfig::default(),
        }
    }
}

fn matches(pattern: &str, name: &str) -> bool {
    match pattern.strip_suffix('*') {
        Some(prefix) => name.starts_with(prefix),
        None => pattern == name,
    }
}

impl Policy {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("cannot read policy file {}", path.display()))?;
        Self::from_yaml(&text).with_context(|| format!("invalid policy file {}", path.display()))
    }

    pub fn from_yaml(text: &str) -> Result<Self> {
        Ok(serde_saphyr::from_str(text)?)
    }

    /// `tainted`: the run has read external content. `read_private`: it has
    /// read the operator's own data.
    pub fn evaluate(
        &self,
        tool: &str,
        profile: &str,
        tainted: bool,
        read_private: bool,
    ) -> Decision {
        let rule = self.rules.iter().find(|r| {
            matches(&r.tool, tool)
                && (r.profiles.is_empty() || r.profiles.iter().any(|p| p == profile))
                && r.tainted.is_none_or(|t| t == tainted)
        });
        let (effect, reason) = match rule {
            Some(r) => (
                r.effect,
                r.reason
                    .clone()
                    .unwrap_or_else(|| format!("policy rule for '{}'", r.tool)),
            ),
            None => (self.default, "policy default".to_string()),
        };
        let decision = to_decision(effect, reason);
        if decision != Decision::Allow {
            return decision;
        }
        if tainted && self.taint.effects.iter().any(|e| matches(e, tool)) {
            return to_decision(
                self.taint.on_effect,
                "the run has read external content and this tool has side effects".into(),
            );
        }
        if tainted && read_private && self.taint.egress.iter().any(|e| matches(e, tool)) {
            return to_decision(
                self.taint.on_effect,
                "the run has read external content and private data, and this tool can send data out"
                    .into(),
            );
        }
        Decision::Allow
    }

    /// True when the tool's output is the operator's own data.
    pub fn reads_private(&self, tool: &str) -> bool {
        self.taint.private_sources.iter().any(|s| matches(s, tool))
    }

    /// True when the tool's output counts as external content.
    pub fn taints(&self, tool: &str) -> bool {
        self.taint.sources.iter().any(|s| matches(s, tool))
    }
}

fn to_decision(effect: Effect, reason: String) -> Decision {
    match effect {
        Effect::Allow => Decision::Allow,
        Effect::Deny => Decision::Deny(reason),
        Effect::RequireApproval => Decision::RequireApproval(reason),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FILE: &str = r#"
default: deny
rules:
  - tool: read_file
    effect: allow
  - tool: write_file
    profiles: [writer]
    effect: require_approval
    reason: writes need a human
  - tool: fetch_url
    tainted: false
    effect: allow
  - tool: "list_*"
    effect: allow
"#;

    #[test]
    fn first_matching_rule_wins_and_default_applies() {
        let p = Policy::from_yaml(FILE).unwrap();
        assert_eq!(
            p.evaluate("read_file", "any", false, false),
            Decision::Allow
        );
        assert_eq!(p.evaluate("list_dir", "any", false, false), Decision::Allow);
        assert_eq!(
            p.evaluate("write_file", "writer", false, false),
            Decision::RequireApproval("writes need a human".into())
        );
        assert!(matches!(
            p.evaluate("write_file", "planner", false, false),
            Decision::Deny(_)
        ));
        assert!(matches!(
            p.evaluate("run_program", "writer", false, false),
            Decision::Deny(_)
        ));
    }

    #[test]
    fn tainted_condition_selects_rules() {
        let p = Policy::from_yaml(FILE).unwrap();
        assert_eq!(p.evaluate("fetch_url", "x", false, false), Decision::Allow);
        assert!(matches!(
            p.evaluate("fetch_url", "x", true, false),
            Decision::Deny(_)
        ));
    }

    #[test]
    fn taint_gates_side_effects_by_default() {
        let p = Policy::default();
        assert_eq!(
            p.evaluate("write_file", "default", false, false),
            Decision::Allow
        );
        assert!(matches!(
            p.evaluate("write_file", "default", true, false),
            Decision::RequireApproval(_)
        ));
        assert_eq!(
            p.evaluate("read_file", "default", true, false),
            Decision::Allow
        );
        assert!(p.taints("fetch_url"));
        assert!(!p.taints("list_dir"));
    }

    #[test]
    fn egress_is_gated_only_with_private_data_and_taint() {
        let p = Policy::default();
        // Browsing the web is free...
        assert_eq!(
            p.evaluate("fetch_url", "default", true, false),
            Decision::Allow
        );
        assert_eq!(
            p.evaluate("fetch_url", "default", false, true),
            Decision::Allow
        );
        // ...but not after reading both a page and the operator's files.
        assert!(matches!(
            p.evaluate("fetch_url", "default", true, true),
            Decision::RequireApproval(_)
        ));
        assert!(p.reads_private("read_file") && !p.reads_private("fetch_url"));
    }

    #[test]
    fn deny_beats_taint() {
        let p = Policy::from_yaml("rules:\n  - tool: run_program\n    effect: deny\n").unwrap();
        assert!(matches!(
            p.evaluate("run_program", "default", true, false),
            Decision::Deny(_)
        ));
    }

    #[test]
    fn example_policy_loads() {
        let p = Policy::from_yaml(include_str!("../policy.example.yaml")).unwrap();
        assert_eq!(
            p.evaluate("read_file", "default", false, false),
            Decision::Allow
        );
        assert!(matches!(
            p.evaluate("write_file", "default", false, false),
            Decision::RequireApproval(_)
        ));
        assert!(matches!(
            p.evaluate("run_program", "default", false, false),
            Decision::Deny(_)
        ));
    }
}
