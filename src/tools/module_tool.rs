use anyhow::{bail, Result};
use async_trait::async_trait;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::process::{format_output, run_program};
use super::sandbox::Workspace;
use super::Tool;
use crate::config::SandboxConfig;
use crate::llm::ToolDefinition;
use crate::modules::{ArgvToken, ModuleRegistry, ModuleToolSpec};

const MAX_OUTPUT_BYTES: usize = 256 * 1024;

/// Executes a module-defined tool: the template becomes an argv, each
/// argument one element, run without a shell.
pub struct ModuleTool {
    spec: ModuleToolSpec,
    argv: Vec<ArgvToken>,
    workspace: Workspace,
    timeout: Duration,
    sandbox: SandboxConfig,
}

impl ModuleTool {
    pub fn new(
        spec: ModuleToolSpec,
        workspace: Workspace,
        timeout: Duration,
        sandbox: &SandboxConfig,
    ) -> Result<Self> {
        let argv = spec.argv_template()?;
        Ok(Self {
            spec,
            argv,
            workspace,
            timeout,
            sandbox: sandbox.clone(),
        })
    }

    /// Build the argv for one call; exposed for tests.
    pub fn build_argv(&self, arguments: &Value) -> Result<Vec<String>> {
        if let (Some(props), Some(given)) = (
            self.spec
                .parameters
                .get("properties")
                .and_then(Value::as_object),
            arguments.as_object(),
        ) {
            for key in given.keys() {
                if !props.contains_key(key) {
                    bail!("unknown argument '{key}' for module {}", self.spec.name);
                }
            }
        }
        let mut out = Vec::with_capacity(self.argv.len());
        for tok in &self.argv {
            match tok {
                ArgvToken::Literal(s) => out.push(s.clone()),
                ArgvToken::Param(name) => {
                    let value = match &arguments[name.as_str()] {
                        Value::String(s) => s.clone(),
                        Value::Number(n) => n.to_string(),
                        Value::Null => bail!("Missing '{name}'"),
                        _ => bail!("'{name}' must be a string"),
                    };
                    if value.is_empty() || value.contains('\0') {
                        bail!("Invalid value for '{name}'");
                    }
                    // A leading '-' would be read as an option by the program.
                    if value.starts_with('-') {
                        bail!("'{name}' must not start with '-'");
                    }
                    if self.spec.path_params.iter().any(|p| p == name) {
                        let path = self.workspace.resolve(&value)?;
                        out.push(path.to_string_lossy().into_owned());
                    } else {
                        out.push(value);
                    }
                }
            }
        }
        Ok(out)
    }
}

#[async_trait]
impl Tool for ModuleTool {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: self.spec.name.clone(),
            description: self.spec.description.clone(),
            parameters: self.spec.parameters.clone(),
        }
    }

    async fn execute(&self, arguments: &Value) -> Result<String> {
        let argv = self.build_argv(arguments)?;
        let out = run_program(
            &argv[0],
            &argv[1..],
            self.workspace.root(),
            self.timeout,
            MAX_OUTPUT_BYTES,
            &self.sandbox,
        )
        .await?;
        if !out.status.success() {
            bail!("Command failed: {}", format_output(&out));
        }
        Ok(format_output(&out))
    }
}

/// Lists all modules and their installation status (read-only).
pub struct ListModulesTool {
    registry: Arc<Mutex<ModuleRegistry>>,
}

impl ListModulesTool {
    pub fn new(registry: Arc<Mutex<ModuleRegistry>>) -> Self {
        Self { registry }
    }
}

#[async_trait]
impl Tool for ListModulesTool {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: "list_modules".into(),
            description: "List available modules and whether they are installed. Modules are \
                          installed by an operator, not by the agent."
                .into(),
            parameters: json!({ "type": "object", "properties": {} }),
        }
    }

    async fn execute(&self, _: &Value) -> Result<String> {
        let reg = self.registry.lock().unwrap();
        Ok(reg.listing())
    }
}
