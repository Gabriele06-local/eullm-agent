use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::PathBuf;

pub mod builtin;

#[derive(Debug, Clone)]
pub struct ModuleToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
    /// Program and arguments, split on whitespace. A token that is exactly
    /// `{arg_name}` is replaced by that argument as a single argv element;
    /// nothing is ever passed to a shell.
    pub command: String,
    /// Arguments that name files: resolved inside the workspace.
    pub path_params: Vec<String>,
}

/// One token of a parsed command template.
#[derive(Debug, Clone, PartialEq)]
pub enum ArgvToken {
    Literal(String),
    Param(String),
}

impl ModuleToolSpec {
    /// Split the template into argv tokens. A placeholder must be a whole
    /// token: `--file={path}` is rejected because it cannot be passed safely.
    pub fn argv_template(&self) -> anyhow::Result<Vec<ArgvToken>> {
        let mut out = Vec::new();
        for tok in self.command.split_whitespace() {
            if let Some(name) = tok.strip_prefix('{').and_then(|t| t.strip_suffix('}')) {
                if name.is_empty() || name.contains(['{', '}']) {
                    anyhow::bail!("invalid placeholder '{tok}' in module {}", self.name);
                }
                out.push(ArgvToken::Param(name.to_string()));
            } else if tok.contains(['{', '}']) {
                anyhow::bail!(
                    "placeholder inside a larger token '{tok}' in module {}",
                    self.name
                );
            } else {
                out.push(ArgvToken::Literal(tok.to_string()));
            }
        }
        match out.first() {
            Some(ArgvToken::Literal(_)) => Ok(out),
            _ => anyhow::bail!("module {} must start with a program name", self.name),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ModuleManifest {
    pub name: String,
    pub description: String,
    pub version: &'static str,
    pub install_linux: Vec<String>,
    pub install_macos: Vec<String>,
    pub install_windows: Vec<String>,
    pub tools: Vec<ModuleToolSpec>,
}

impl ModuleManifest {
    pub fn install_commands(&self) -> &[String] {
        #[cfg(target_os = "macos")]
        return &self.install_macos;
        #[cfg(target_os = "windows")]
        return &self.install_windows;
        #[cfg(not(any(target_os = "macos", target_os = "windows")))]
        return &self.install_linux;
    }
}

#[derive(Debug, Serialize, Deserialize, Default)]
pub struct ModuleState {
    pub installed: HashSet<String>,
}

pub struct ModuleRegistry {
    pub manifests: Vec<ModuleManifest>,
    pub state: ModuleState,
    pub state_path: PathBuf,
}

impl ModuleRegistry {
    pub fn load(state_path: PathBuf) -> Result<Self> {
        let state = if state_path.exists() {
            let text = std::fs::read_to_string(&state_path)?;
            serde_json::from_str(&text)?
        } else {
            ModuleState::default()
        };
        Ok(Self {
            manifests: builtin::all_modules(),
            state,
            state_path,
        })
    }

    pub fn save_state(&self) -> Result<()> {
        if let Some(parent) = self.state_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&self.state_path, serde_json::to_string_pretty(&self.state)?)?;
        Ok(())
    }

    /// Short text injected into the system prompt so the LLM knows what it has.
    pub fn status_summary(&self) -> String {
        let installed: Vec<&str> = self
            .manifests
            .iter()
            .filter(|m| self.state.installed.contains(&m.name))
            .map(|m| m.name.as_str())
            .collect();
        let not_installed: Vec<&str> = self
            .manifests
            .iter()
            .filter(|m| !self.state.installed.contains(&m.name))
            .map(|m| m.name.as_str())
            .collect();

        let mut s = String::new();
        if !installed.is_empty() {
            s.push_str(&format!("\n\nInstalled modules: {}", installed.join(", ")));
        }
        if !not_installed.is_empty() {
            s.push_str(&format!(
                "\nAvailable modules (not installed): {}",
                not_installed.join(", ")
            ));
            s.push_str("\nAn operator can install them with `eullm-agent module install <name>`.");
        }
        s
    }

    /// Human-readable list of modules, installed first.
    pub fn listing(&self) -> String {
        let mut out = String::from("=== Installed modules ===\n");
        let mut any = false;
        for m in &self.manifests {
            if self.state.installed.contains(&m.name) {
                any = true;
                out.push_str(&format!(
                    "  {} v{} — {}\n",
                    m.name, m.version, m.description
                ));
                for t in &m.tools {
                    out.push_str(&format!("    tool: {}\n", t.name));
                }
            }
        }
        if !any {
            out.push_str("  (none)\n");
        }
        out.push_str("\n=== Available (not installed) ===\n");
        let mut any = false;
        for m in &self.manifests {
            if !self.state.installed.contains(&m.name) {
                any = true;
                out.push_str(&format!("  {} — {}\n", m.name, m.description));
            }
        }
        if !any {
            out.push_str("  (all modules installed)\n");
        }
        out
    }

    /// Run a module's install commands and record it as installed. Called
    /// only from the operator's CLI, never by the agent.
    pub fn install(&mut self, name: &str) -> Result<()> {
        let manifest = self
            .manifests
            .iter()
            .find(|m| m.name == name)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("Unknown module '{name}'"))?;
        if self.state.installed.contains(name) {
            return Ok(());
        }
        for cmd in manifest.install_commands() {
            println!("$ {cmd}");
            #[cfg(target_os = "windows")]
            let status = std::process::Command::new("cmd")
                .arg("/C")
                .arg(cmd)
                .status()?;
            #[cfg(not(target_os = "windows"))]
            let status = std::process::Command::new("sh")
                .arg("-c")
                .arg(cmd)
                .status()?;
            if !status.success() {
                anyhow::bail!("install failed at '{cmd}'");
            }
        }
        self.state.installed.insert(name.to_string());
        self.save_state()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_templates_parse() {
        for m in builtin::all_modules() {
            for t in &m.tools {
                t.argv_template().unwrap();
            }
        }
    }

    #[test]
    fn install_runs_its_commands_only_once() {
        let dir = std::env::temp_dir().join(format!("eullm-modules-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join("install-count");
        let cmd = format!("echo x >> {}", marker.display());
        let mut reg = ModuleRegistry {
            manifests: vec![ModuleManifest {
                name: "x".into(),
                description: String::new(),
                version: "1",
                install_linux: vec![cmd.clone()],
                install_macos: vec![cmd.clone()],
                install_windows: vec![cmd],
                tools: vec![],
            }],
            state: ModuleState::default(),
            state_path: dir.join("state.json"),
        };
        reg.install("x").unwrap();
        reg.install("x").unwrap();
        assert_eq!(std::fs::read_to_string(&marker).unwrap().lines().count(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn placeholders_inside_tokens_are_rejected() {
        let spec = ModuleToolSpec {
            name: "x".into(),
            description: String::new(),
            parameters: serde_json::json!({}),
            command: "prog --file={path}".into(),
            path_params: vec![],
        };
        assert!(spec.argv_template().is_err());
    }
}
