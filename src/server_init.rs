//! `eullm-agent server-init`: the first start of the server installation
//! (deploy/server) in one command. It writes what is missing in the folder
//! — compose file, `.env` with every password and token generated, `core.yaml`
//! with the provider chosen — starts the stack and creates the first
//! newsroom with its owner's login. What exists already is never changed, so
//! running it again only starts the stack.

use anyhow::{bail, Context, Result};
use base64::Engine;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::wizard::{prompt, prompt_required, write_private};

const COMPOSE: &str = include_str!("../deploy/server/compose.yaml");
const ROLES: &str = include_str!("../deploy/server/initdb/10-roles.sh");
const ENV: &str = include_str!("../deploy/server/.env.example");
const CORE: &str = include_str!("../deploy/server/core.example.yaml");

/// The provider block of `core.example.yaml` that the choice replaces.
const EXAMPLE_PROVIDER: &str =
    "# The model. EuLLM Engine on this machine:\nprovider:\n  type: eullm\n  base_url: http://host.docker.internal:11434\n  model: default\n";

/// The model the server will use.
#[derive(Debug, Clone, PartialEq)]
pub enum Provider {
    /// NVIDIA Build: free for trials and development, a reasoning model.
    Nvidia { model: String },
    /// Groq, free tier.
    Groq { model: String },
    /// EuLLM Engine on this machine.
    Eullm { model: String },
    /// Any other OpenAI-compatible service.
    OpenAi { base_url: String, model: String },
}

impl Provider {
    fn needs_key(&self) -> bool {
        !matches!(self, Provider::Eullm { .. })
    }

    /// The `provider:` block (and `pricing:` for the free services).
    pub fn yaml(&self) -> String {
        let openai = |base_url: &str, model: &str, extra: &str| {
            format!(
                "provider:\n  type: openai\n  base_url: {base_url}\n  model: {model}\n  api_key_env: LLM_API_KEY\n{extra}"
            )
        };
        match self {
            Provider::Nvidia { model } => openai(
                "https://integrate.api.nvidia.com/v1",
                model,
                "  max_tokens: 16384   # the reasoning counts too: without it the answer can be empty\n\
                 pricing: {input_per_mtok: 0.0, output_per_mtok: 0.0}\n",
            ),
            Provider::Groq { model } => openai(
                "https://api.groq.com/openai/v1",
                model,
                "pricing: {input_per_mtok: 0.0, output_per_mtok: 0.0}\n",
            ),
            Provider::Eullm { model } => format!(
                "provider:\n  type: eullm\n  base_url: http://host.docker.internal:11434\n  model: {model}\n"
            ),
            Provider::OpenAi { base_url, model } => openai(base_url, model, ""),
        }
    }
}

/// `core.yaml`: the example with the chosen provider.
pub fn core_yaml(provider: &Provider) -> String {
    CORE.replacen(
        EXAMPLE_PROVIDER,
        &format!("# The model, chosen at server-init:\n{}", provider.yaml()),
        1,
    )
}

/// The compose file for a folder without the sources: images only.
pub fn compose_without_build() -> String {
    COMPOSE
        .lines()
        .filter(|l| !l.trim_start().starts_with("build:"))
        .map(|l| format!("{l}\n"))
        .collect()
}

fn random(n: usize) -> Result<Vec<u8>> {
    let mut buf = vec![0u8; n];
    getrandom::fill(&mut buf).map_err(|e| anyhow::anyhow!("no randomness from the system: {e}"))?;
    Ok(buf)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `.env`: the example with every password and token generated, the release
/// of the images and the model key filled in.
pub fn env_file(version: &str, llm_api_key: &str) -> Result<String> {
    let mut out = String::new();
    for line in ENV.lines() {
        let value = match line {
            "POSTGRES_PASSWORD="
            | "CORE_DB_PASSWORD="
            | "EDITOR_OWNER_DB_PASSWORD="
            | "EDITOR_WEB_DB_PASSWORD="
            | "EDITOR_CORE_TOKEN="
            | "CORE_ADMIN_TOKEN=" => Some(hex(&random(32)?)),
            "EDITOR_SECRET_KEY=" => {
                Some(base64::engine::general_purpose::STANDARD.encode(random(32)?))
            }
            "EULLM_VERSION=" => Some(version.to_string()),
            "LLM_API_KEY=" => Some(llm_api_key.to_string()),
            _ => None,
        };
        match value {
            Some(v) => out.push_str(&format!("{line}{v}\n")),
            None => out.push_str(&format!("{line}\n")),
        }
    }
    Ok(out)
}

/// What the command line already says; the rest is asked.
#[derive(Debug, Default)]
pub struct Options {
    pub dir: PathBuf,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub base_url: Option<String>,
    pub no_start: bool,
}

fn pick_provider(opts: &Options) -> Result<Provider> {
    let choice = match &opts.provider {
        Some(p) => p.clone(),
        None => {
            println!("The model for the newsrooms:");
            println!("  1. NVIDIA Build (free for trials, not for production)  [default]");
            println!("  2. Groq (free tier)");
            println!("  3. EuLLM Engine on this machine");
            println!("  4. Another OpenAI-compatible service");
            prompt("Provider", "1")?
        }
    };
    let model = |default: &str| -> Result<String> {
        match &opts.model {
            Some(m) => Ok(m.clone()),
            None => prompt("Model", default),
        }
    };
    Ok(match choice.trim() {
        "1" | "nvidia" => Provider::Nvidia {
            model: model("nvidia/nemotron-3.5-super-vl-120b-a12b")?,
        },
        "2" | "groq" => Provider::Groq {
            model: model("openai/gpt-oss-120b")?,
        },
        "3" | "eullm" => Provider::Eullm {
            model: model("default")?,
        },
        "4" | "openai" => Provider::OpenAi {
            base_url: match &opts.base_url {
                Some(u) => u.clone(),
                None => prompt_required("Base URL (for example https://api.openai.com/v1)")?,
            },
            model: match &opts.model {
                Some(m) => m.clone(),
                None => prompt_required("Model")?,
            },
        },
        other => bail!("unknown provider '{other}': use nvidia, groq, eullm or openai"),
    })
}

fn prompt_secret(label: &str) -> Result<String> {
    use std::io::Write;
    loop {
        print!("{label}: ");
        std::io::stdout().flush()?;
        #[cfg(unix)]
        let saved = {
            let mut t: libc::termios = unsafe { std::mem::zeroed() };
            // SAFETY: tcgetattr/tcsetattr on stdin with a termios we own.
            if unsafe { libc::tcgetattr(0, &mut t) } == 0 {
                let mut quiet = t;
                quiet.c_lflag &= !libc::ECHO;
                unsafe { libc::tcsetattr(0, libc::TCSANOW, &quiet) };
                Some(t)
            } else {
                None
            }
        };
        let mut buf = String::new();
        let read = std::io::stdin().read_line(&mut buf);
        #[cfg(unix)]
        if let Some(t) = saved {
            unsafe { libc::tcsetattr(0, libc::TCSANOW, &t) };
            println!();
        }
        read?;
        let v = buf.trim().to_string();
        if !v.is_empty() {
            return Ok(v);
        }
        println!("  (required \u{2014} please enter a value)");
    }
}

fn compose(dir: &Path, args: &[&str]) -> Command {
    let mut c = Command::new("docker");
    c.arg("compose").args(args).current_dir(dir);
    c
}

fn run(mut c: Command, what: &str) -> Result<String> {
    let out = c
        .output()
        .with_context(|| format!("cannot run docker ({what})"))?;
    if !out.status.success() {
        bail!(
            "{what} failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

pub fn server_init(opts: Options) -> Result<()> {
    let dir = &opts.dir;
    std::fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))?;
    let version = env!("CARGO_PKG_VERSION");
    let mut first_run = false;

    if !dir.join("compose.yaml").exists() {
        std::fs::write(dir.join("compose.yaml"), compose_without_build())?;
        let initdb = dir.join("initdb");
        std::fs::create_dir_all(&initdb)?;
        std::fs::write(initdb.join("10-roles.sh"), ROLES)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(
                initdb.join("10-roles.sh"),
                std::fs::Permissions::from_mode(0o755),
            )?;
        }
        println!("written: compose.yaml, initdb/10-roles.sh (images of release {version})");
    }

    let core_path = dir.join("core.yaml");
    let env_path = dir.join(".env");
    let mut provider = None;
    if !core_path.exists() {
        let p = pick_provider(&opts)?;
        // No secret in it (the key is in .env), and the Core's container
        // runs as a user of its own that must read it.
        std::fs::write(&core_path, core_yaml(&p))?;
        println!("written: core.yaml");
        provider = Some(p);
    }
    if !env_path.exists() {
        let needs_key = provider.as_ref().map(Provider::needs_key).unwrap_or(true);
        let key = match std::env::var("LLM_API_KEY") {
            Ok(k) if !k.is_empty() => k,
            _ if needs_key => prompt_secret("API key of the model (LLM_API_KEY, not shown)")?,
            _ => String::new(),
        };
        write_private(&env_path, &env_file(version, &key)?)?;
        println!("written: .env (passwords and tokens generated, readable only by you)");
        first_run = true;
    }

    if opts.no_start {
        println!("\nStart with: cd {} && docker compose up -d", dir.display());
        return Ok(());
    }
    run(compose(dir, &["version"]), "docker compose").context(
        "Docker with the compose plugin is needed: https://docs.docker.com/engine/install/",
    )?;
    println!("\nStarting PostgreSQL, the Core and Editor Mode (the first time the images are downloaded)...");
    let status = compose(dir, &["up", "-d"])
        .status()
        .context("cannot run docker compose")?;
    if !status.success() {
        bail!("docker compose up failed: see the messages above");
    }
    // The editor schema is created by editor-migrate at start: wait for it,
    // then ask Editor Mode for the first-start link, while no administrator
    // exists (a run that stopped halfway gets a fresh link).
    let mut out = None;
    let mut last = String::new();
    for _ in 0..60 {
        match run(
            compose(dir, &["exec", "-T", "editor-web", "editor", "setup-link"]),
            "asking for the first-start link",
        ) {
            Ok(o) => {
                out = Some(o);
                break;
            }
            Err(e) => last = e.to_string(),
        }
        std::thread::sleep(std::time::Duration::from_secs(3));
    }
    let Some(out) = out else {
        bail!(
            "{last}\nEditor Mode is not ready: see `docker compose logs editor-migrate editor-web`"
        );
    };
    // The port docker compose used: the shell's, else the one in .env.
    let port = std::env::var("EDITOR_PORT")
        .ok()
        .filter(|p| !p.is_empty())
        .or_else(|| {
            std::fs::read_to_string(&env_path).ok().and_then(|t| {
                t.lines()
                    .find_map(|l| l.strip_prefix("EDITOR_PORT=").map(|v| v.trim().to_string()))
                    .filter(|p| !p.is_empty())
            })
        });
    let port = port.unwrap_or_else(|| "8090".into());
    let base = format!("http://127.0.0.1:{port}");
    println!(
        "\n{}",
        if first_run {
            "Started."
        } else {
            "Started. The files that already existed were left as they were."
        }
    );
    match out.split_once("/setup/") {
        Some((_, rest)) => {
            let token = rest.split_whitespace().next().unwrap_or_default();
            println!("\nCreate your administrator account: open this link (it works once)");
            println!("  {base}/setup/{token}");
            println!("From there (/admin) you create the newsrooms, give them plans and limits");
            println!("and invite their owners; /editor opens a newsroom.");
        }
        None => println!("\nSign in at {base} (administration: {base}/admin)."),
    }
    println!("  Put a reverse proxy with HTTPS in front to reach it from outside.");
    println!("\nKeep a copy of {} somewhere safe:", env_path.display());
    println!("  EDITOR_SECRET_KEY in it encrypts the publishing credentials; without it they must be entered again.");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_example_provider_is_replaced() {
        assert!(
            CORE.contains(EXAMPLE_PROVIDER),
            "core.example.yaml changed: update EXAMPLE_PROVIDER"
        );
        let y = core_yaml(&Provider::Nvidia { model: "m".into() });
        assert!(
            y.contains("base_url: https://integrate.api.nvidia.com/v1")
                && y.contains("max_tokens: 16384")
        );
        assert!(!y.contains("type: eullm\n  base_url: http://host.docker.internal"));
        assert!(y.contains("delegate: true"));
    }

    #[test]
    fn every_provider_gives_a_config_that_loads() {
        for p in [
            Provider::Nvidia { model: "m".into() },
            Provider::Groq { model: "m".into() },
            Provider::Eullm { model: "m".into() },
            Provider::OpenAi {
                base_url: "https://x.example/v1".into(),
                model: "m".into(),
            },
        ] {
            let dir =
                std::env::temp_dir().join(format!("eullm-init-{}", uuid::Uuid::new_v4().simple()));
            std::fs::create_dir_all(&dir).unwrap();
            let path = dir.join("core.yaml");
            std::fs::write(&path, core_yaml(&p)).unwrap();
            crate::config::Config::load(&path).unwrap_or_else(|e| panic!("{p:?}: {e:#}"));
            std::fs::remove_dir_all(&dir).ok();
        }
    }

    #[test]
    fn the_env_has_every_secret_and_they_differ() {
        let a = env_file("0.5.4", "nvapi-x").unwrap();
        let b = env_file("0.5.4", "nvapi-x").unwrap();
        assert_ne!(a, b);
        let get = |k: &str| {
            a.lines()
                .find_map(|l| l.strip_prefix(&format!("{k}=")))
                .unwrap()
                .to_string()
        };
        for k in [
            "POSTGRES_PASSWORD",
            "CORE_DB_PASSWORD",
            "EDITOR_OWNER_DB_PASSWORD",
            "EDITOR_WEB_DB_PASSWORD",
            "EDITOR_CORE_TOKEN",
            "CORE_ADMIN_TOKEN",
        ] {
            assert_eq!(get(k).len(), 64, "{k}");
        }
        let key = base64::engine::general_purpose::STANDARD
            .decode(get("EDITOR_SECRET_KEY"))
            .unwrap();
        assert_eq!(key.len(), 32);
        assert_eq!(get("EULLM_VERSION"), "0.5.4");
        assert_eq!(get("LLM_API_KEY"), "nvapi-x");
        assert_eq!(get("BIND"), "127.0.0.1");
    }

    #[test]
    fn the_compose_without_sources_has_no_build() {
        let c = compose_without_build();
        assert!(!c.contains("build:") && c.contains("image: ghcr.io/eullm/eullm-agent:"));
        assert_eq!(
            c.matches("image: ghcr.io/eullm/eullm-agent-editor:")
                .count(),
            3
        );
    }
}
