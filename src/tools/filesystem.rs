use anyhow::{bail, Result};
use async_trait::async_trait;
use serde_json::{json, Value};
use tokio::fs;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use super::sandbox::Workspace;
use super::Tool;
use crate::llm::ToolDefinition;

const MAX_DIR_ENTRIES: usize = 1000;

// --- read_file ---

pub struct ReadFileTool {
    workspace: Workspace,
    max_bytes: usize,
}

impl ReadFileTool {
    pub fn new(workspace: Workspace, max_bytes: usize) -> Self {
        Self {
            workspace,
            max_bytes,
        }
    }
}

#[async_trait]
impl Tool for ReadFileTool {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: "read_file".into(),
            description: "Read the text contents of a file in the workspace.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string", "description": "Path relative to the workspace" }
                },
                "required": ["path"]
            }),
        }
    }

    async fn execute(&self, arguments: &Value) -> Result<String> {
        let input = arguments["path"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing 'path'"))?;
        let path = self.workspace.resolve(input)?;
        let file = fs::File::open(&path)
            .await
            .map_err(|e| anyhow::anyhow!("read_file {input}: {e}"))?;
        let mut data = Vec::new();
        file.take(self.max_bytes as u64 + 1)
            .read_to_end(&mut data)
            .await?;
        let truncated = data.len() > self.max_bytes;
        if truncated {
            // Back up only when the cut splits a character; a file that is
            // not UTF-8 keeps its full limit.
            let cut = match std::str::from_utf8(&data[..self.max_bytes]) {
                Err(e) if e.error_len().is_none() => e.valid_up_to(),
                _ => self.max_bytes,
            };
            data.truncate(cut);
        }
        let mut text = String::from_utf8_lossy(&data).into_owned();
        if truncated {
            text.push_str(&format!("\n[truncated at {} bytes]", self.max_bytes));
        }
        Ok(text)
    }
}

// --- write_file ---

pub struct WriteFileTool {
    workspace: Workspace,
    max_bytes: usize,
}

impl WriteFileTool {
    pub fn new(workspace: Workspace, max_bytes: usize) -> Self {
        Self {
            workspace,
            max_bytes,
        }
    }
}

#[async_trait]
impl Tool for WriteFileTool {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: "write_file".into(),
            description: "Write (create or overwrite) a text file in the workspace.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string", "description": "Destination path relative to the workspace" },
                    "content": { "type": "string", "description": "File content to write" }
                },
                "required": ["path", "content"]
            }),
        }
    }

    async fn execute(&self, arguments: &Value) -> Result<String> {
        let input = arguments["path"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing 'path'"))?;
        let content = arguments["content"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing 'content'"))?;
        if content.len() > self.max_bytes {
            bail!("Content too large (max {} bytes)", self.max_bytes);
        }
        let path = self.workspace.resolve_for_write(input)?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).await?;
            // Re-check after creating directories: nothing may have moved us out.
            let parent = parent.canonicalize()?;
            if !parent.starts_with(self.workspace.root()) {
                bail!("Access denied: path outside the workspace");
            }
        }

        let mut opts = fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        opts.custom_flags(libc::O_NOFOLLOW);
        let mut file = opts
            .open(&path)
            .await
            .map_err(|e| anyhow::anyhow!("write_file {input}: {e}"))?;
        file.write_all(content.as_bytes()).await?;
        file.flush().await?;

        Ok(format!("Written {} bytes to {input}", content.len()))
    }
}

// --- list_dir ---

pub struct ListDirTool {
    workspace: Workspace,
}

impl ListDirTool {
    pub fn new(workspace: Workspace) -> Self {
        Self { workspace }
    }
}

#[async_trait]
impl Tool for ListDirTool {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: "list_dir".into(),
            description: "List the entries of a directory in the workspace.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": { "type": "string", "description": "Directory path relative to the workspace (default: .)" }
                }
            }),
        }
    }

    async fn execute(&self, arguments: &Value) -> Result<String> {
        let input = arguments["path"].as_str().unwrap_or(".");
        let path = self.workspace.resolve(input)?;
        let mut entries = fs::read_dir(&path)
            .await
            .map_err(|e| anyhow::anyhow!("list_dir {input}: {e}"))?;

        let mut names = Vec::new();
        let mut more = false;
        while let Some(entry) = entries.next_entry().await? {
            if names.len() == MAX_DIR_ENTRIES {
                more = true;
                break;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            let is_dir = entry.file_type().await.map(|t| t.is_dir()).unwrap_or(false);
            names.push(if is_dir { format!("{name}/") } else { name });
        }
        names.sort();
        if more {
            names.push(format!("[listing stopped at {MAX_DIR_ENTRIES} entries]"));
        }
        Ok(if names.is_empty() {
            "(empty)".into()
        } else {
            names.join("\n")
        })
    }
}
