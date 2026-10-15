//! Editor Mode next to `eullm-agent start`: when the local installation put
//! it in `<config dir>/editor-mode` (with its `.venv`), `start` runs its
//! dashboard and its hourly worker on this computer, with the data in
//! `<config dir>/editor.db` and a Core token made at every start.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use anyhow::{bail, Context, Result};
use tokio::process::{Child, Command};

pub const LISTEN: &str = "127.0.0.1:8090";
const TENANT: &str = "local";

pub struct EditorMode {
    home: PathBuf,
    exe: PathBuf,
}

/// Editor Mode as the local installation leaves it, if it is there.
pub fn find(home: &Path) -> Option<EditorMode> {
    let venv = home.join("editor-mode").join(".venv");
    let exe = if cfg!(windows) {
        venv.join("Scripts").join("editor.exe")
    } else {
        venv.join("bin").join("editor")
    };
    exe.is_file().then(|| EditorMode {
        home: home.to_path_buf(),
        exe,
    })
}

/// The running dashboard and worker; dropping it stops both.
pub struct Running {
    _children: Vec<Child>,
    pub url: String,
    pub login_token: String,
    pub logs: PathBuf,
}

impl EditorMode {
    fn command(&self, core_url: &str, core_token: &str) -> Command {
        let mut cmd = Command::new(&self.exe);
        cmd.env(
            "EDITOR_DATABASE_URL",
            format!(
                "sqlite:///{}",
                self.home
                    .join("editor.db")
                    .display()
                    .to_string()
                    .replace('\\', "/")
            ),
        )
        .env_remove("EDITOR_ADMIN_DATABASE_URL")
        .env("EDITOR_CORE_URL", core_url)
        .env("EDITOR_CORE_TOKEN", core_token)
        .current_dir(&self.home)
        .kill_on_drop(true);
        cmd
    }

    async fn run(&self, core_url: &str, core_token: &str, args: &[&str]) -> Result<String> {
        let out = self
            .command(core_url, core_token)
            .args(args)
            .stdin(Stdio::null())
            .output()
            .await
            .with_context(|| format!("cannot run {}", self.exe.display()))?;
        if !out.status.success() {
            bail!(
                "editor {} failed: {}",
                args.join(" "),
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    /// Prepare the database, then start the dashboard and the worker.
    pub async fn launch(&self, core_url: &str, core_token: &str) -> Result<Running> {
        self.run(core_url, core_token, &["migrate"]).await?;
        self.run(
            core_url,
            core_token,
            &["tenant-add", TENANT, "--name", "Locale"],
        )
        .await?;
        let login_token = self.login_token(core_url, core_token).await?;

        let logs = self.home.join("logs");
        std::fs::create_dir_all(&logs)?;
        let (host, port) = LISTEN.split_once(':').expect("LISTEN has a port");
        let mut children = Vec::new();
        for (name, args) in [
            (
                "editor-serve",
                vec!["serve", "--host", host, "--port", port],
            ),
            ("editor-worker", vec!["worker"]),
        ] {
            let log = std::fs::File::create(logs.join(format!("{name}.log")))?;
            let child = self
                .command(core_url, core_token)
                .args(&args)
                .stdin(Stdio::null())
                .stdout(log.try_clone()?)
                .stderr(log)
                .spawn()
                .with_context(|| format!("cannot start {name}"))?;
            children.push(child);
        }
        Ok(Running {
            _children: children,
            url: format!("http://{LISTEN}/"),
            login_token,
            logs,
        })
    }

    /// The owner's dashboard token, made once and kept in `editor-token`
    /// (readable by this user only): Editor Mode stores only its hash.
    async fn login_token(&self, core_url: &str, core_token: &str) -> Result<String> {
        let path = self.home.join("editor-token");
        if let Ok(t) = std::fs::read_to_string(&path) {
            if !t.trim().is_empty() {
                return Ok(t.trim().to_string());
            }
        }
        let out = self
            .run(
                core_url,
                core_token,
                &["token-new", "--tenant", TENANT, "--name", "owner"],
            )
            .await?;
        let token = out
            .lines()
            .map(str::trim)
            .rfind(|l| l.starts_with("edt_"))
            .context("editor token-new printed no token")?
            .to_string();
        write_private(&path, &token)?;
        Ok(token)
    }
}

fn write_private(path: &Path, content: &str) -> Result<()> {
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(path)?;
        f.write_all(content.as_bytes())?;
    }
    #[cfg(not(unix))]
    std::fs::write(path, content)?;
    Ok(())
}
