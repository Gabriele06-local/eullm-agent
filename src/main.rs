use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tracing::warn;
use tracing_subscriber::EnvFilter;

use eullm_agent::approvals::TerminalApprover;
use eullm_agent::audit::Audit;
use eullm_agent::config::{ApiTokenConfig, Config, ProviderConfig};
use eullm_agent::modules::ModuleRegistry;
use eullm_agent::{agent, api, setup, telegram, wizard};

#[derive(Parser)]
#[command(name = "eullm-agent", version, about = "EULLM autonomous task agent")]
struct Cli {
    #[arg(short, long, default_value = "config.yaml")]
    config: PathBuf,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the Telegram bot and wait for tasks
    Serve,
    /// Run a one-shot task from the CLI and print the result
    Run {
        /// Task description
        task: String,
    },
    /// Start the Core HTTP API (runs, model calls, approvals)
    Api {
        /// Address to listen on (overrides api.listen)
        #[arg(long)]
        listen: Option<String>,
    },
    /// Start the local app: web interface on this computer, data in one file
    Start {
        /// Address to listen on; only this computer (loopback) is allowed
        #[arg(long, default_value = "127.0.0.1:8088")]
        listen: String,
        /// Do not open the browser
        #[arg(long)]
        no_browser: bool,
    },
    /// List or install modules (operator only; the agent cannot install them)
    Module {
        #[command(subcommand)]
        action: ModuleAction,
    },
    /// Manage API tokens
    Token {
        #[command(subcommand)]
        action: TokenAction,
    },
    /// First start of the server installation: writes what is missing in the
    /// folder (compose file, .env with generated passwords, core.yaml), starts
    /// it with Docker and creates the first newsroom with its login
    ServerInit {
        /// Folder of the installation; deploy/server in a checkout of the sources
        #[arg(long, default_value = "eullm-server")]
        dir: PathBuf,
        /// nvidia, groq, eullm or openai (asked when missing)
        #[arg(long)]
        provider: Option<String>,
        /// Model name (asked when missing)
        #[arg(long)]
        model: Option<String>,
        /// Base URL, for --provider openai
        #[arg(long)]
        base_url: Option<String>,
        /// Write the files only, do not start
        #[arg(long)]
        no_start: bool,
    },
}

#[derive(Subcommand)]
enum TokenAction {
    /// Print a new random token and the SHA-256 to put in api.tokens
    New,
}

#[derive(Subcommand)]
enum ModuleAction {
    /// Show available and installed modules
    List,
    /// Run a module's install commands and enable its tools
    Install {
        name: String,
        /// Do not ask for confirmation
        #[arg(long)]
        yes: bool,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("eullm_agent=info".parse()?))
        .init();

    let cli = Cli::parse();

    let module_registry = Arc::new(Mutex::new(ModuleRegistry::load(module_state_path())?));

    if let Commands::Module { action } = &cli.command {
        return module_command(action, &module_registry);
    }
    if let Commands::Token {
        action: TokenAction::New,
    } = &cli.command
    {
        let token = api::new_token();
        println!("token:        {token}");
        println!("token_sha256: {}", api::sha256_hex(&token));
        println!("\nGive the token to the client; put only token_sha256 in api.tokens.");
        return Ok(());
    }

    if let Commands::ServerInit {
        dir,
        provider,
        model,
        base_url,
        no_start,
    } = cli.command
    {
        return eullm_agent::server_init::server_init(eullm_agent::server_init::Options {
            dir,
            provider,
            model,
            base_url,
            no_start,
        });
    }

    let mut config = if cli.config.exists() {
        Config::load(&cli.config)
            .with_context(|| format!("Cannot load config from {:?}", cli.config))?
    } else if matches!(cli.command, Commands::Start { .. }) {
        first_local_config(&cli.config).await?
    } else {
        wizard::run(&cli.config)?
    };
    for w in config.deprecation_warnings() {
        warn!("{w}");
    }

    if config.modules.enabled {
        let reg = module_registry.lock().unwrap();
        config.system_prompt.push_str(&reg.status_summary());
    }

    match cli.command {
        Commands::Serve => {
            config.validate_telegram()?;
            let core = setup::build_core(Arc::new(config), Arc::clone(&module_registry)).await?;
            setup::recover_runs(&core, "telegram:").await?;
            telegram::serve(core).await?;
        }
        Commands::Api { listen } => {
            let api_cfg = config
                .api
                .clone()
                .context("missing api section in the config")?;
            let tokens = api_cfg
                .tokens
                .iter()
                .map(api::ApiToken::resolve)
                .collect::<Result<Vec<_>>>()?;
            let listen = listen.unwrap_or(api_cfg.listen);
            let core = setup::build_core(Arc::new(config), Arc::clone(&module_registry)).await?;
            setup::recover_runs(&core, "api:").await?;
            api::serve(core, &listen, tokens).await?;
        }
        Commands::Start { listen, no_browser } => {
            let addr: std::net::SocketAddr = listen.parse().with_context(|| {
                format!("--listen must be an address like 127.0.0.1:8088, not '{listen}'")
            })?;
            if !addr.ip().is_loopback() {
                anyhow::bail!(
                    "start serves this computer only (127.0.0.1); use `eullm-agent api` for a server"
                );
            }
            if config.database.is_none() {
                config.database = Some(wizard::local_database(&cli.config));
            }
            // The tokens of the config (other programs) stay valid; the
            // browser gets a new one at every start, never written to disk.
            let mut tokens = match &config.api {
                Some(a) => a
                    .tokens
                    .iter()
                    .map(api::ApiToken::resolve)
                    .collect::<Result<Vec<_>>>()?,
                None => Vec::new(),
            };
            let secret = api::new_token();
            tokens.push(api::ApiToken::resolve(&ApiTokenConfig {
                name: "browser".into(),
                tenant: "default".into(),
                token_sha256: Some(api::sha256_hex(&secret)),
                token_env: None,
                profiles: Vec::new(),
                delegate: false,
                delegate_limits: None,
            })?);
            // Editor Mode, when the local installation put it next to the
            // config: it reads the web through the Core and gets its own
            // token, also new at every start.
            let editor = eullm_agent::editor_mode::find(&wizard::config_dir(&cli.config));
            let editor_secret = api::new_token();
            if editor.is_some() {
                let api_cfg = config
                    .api
                    .get_or_insert_with(|| serde_json::from_value(serde_json::json!({})).unwrap());
                if api_cfg.fetch.is_none() {
                    api_cfg.fetch = Some(serde_json::from_value(serde_json::json!({}))?);
                }
                tokens.push(api::ApiToken::resolve(&ApiTokenConfig {
                    name: "editor-mode".into(),
                    tenant: "default".into(),
                    token_sha256: Some(api::sha256_hex(&editor_secret)),
                    token_env: None,
                    profiles: Vec::new(),
                    delegate: false,
                    delegate_limits: None,
                })?);
            }
            let core = setup::build_core(Arc::new(config), Arc::clone(&module_registry)).await?;
            setup::recover_runs(&core, "api:").await?;
            let listen = addr.to_string();
            let server = tokio::spawn(async move { api::serve(core, &listen, tokens).await });
            let editor = match editor {
                Some(e) => match e.launch(&format!("http://{addr}"), &editor_secret).await {
                    Ok(r) => Some(r),
                    Err(e) => {
                        warn!("Editor Mode did not start: {e:#}");
                        None
                    }
                },
                None => None,
            };
            let url = format!("http://{addr}/#token={secret}");
            println!("\nEuLLM Agent is ready. Open this link in your browser:\n\n  {url}\n");
            if let Some(e) = &editor {
                println!(
                    "Editor Mode: {}\n  login token: {}\n  logs: {}\n",
                    e.url,
                    e.login_token,
                    e.logs.display()
                );
            }
            println!("The link works until this program stops. Press Ctrl-C to stop.\n");
            if !no_browser {
                open_browser(&url);
            }
            let result = server.await.context("the API stopped")?;
            drop(editor);
            result?;
        }
        Commands::Run { task } => {
            let router = eullm_agent::router::ModelRouter::from_config(&config)?;
            let profile = config.profile("default").unwrap_or_default();
            let model = router
                .get(&profile.model)
                .context("the default profile uses an unknown model")?
                .clone();
            let tools = setup::build_tools(&config, Arc::clone(&module_registry))?;
            let policy = setup::load_policy(&config)?;
            let audit = match &config.audit_log {
                Some(path) => Some(Arc::new(Audit::open(path)?)),
                None => None,
            };
            let system_prompt = profile
                .system_prompt
                .clone()
                .unwrap_or_else(|| config.system_prompt.clone());
            let agent = agent::Agent::new(model.client.as_ref(), &tools, config.max_iterations)
                .with_limits(&config.limits)
                .with_profile("default", &profile)
                .with_pricing(model.pricing)
                .with_policy(policy)
                .with_approver(Arc::new(TerminalApprover))
                .with_audit(audit, "cli");
            let result = agent
                .run(&system_prompt, &task, |s| println!("[\u{2022}] {s}"))
                .await?;
            println!("{result}");
        }
        Commands::Module { .. } | Commands::Token { .. } | Commands::ServerInit { .. } => {
            unreachable!()
        }
    }

    Ok(())
}

fn module_command(action: &ModuleAction, registry: &Arc<Mutex<ModuleRegistry>>) -> Result<()> {
    let mut reg = registry.lock().unwrap();
    match action {
        ModuleAction::List => print!("{}", reg.listing()),
        ModuleAction::Install { name, yes } => {
            let manifest = reg
                .manifests
                .iter()
                .find(|m| &m.name == name)
                .with_context(|| format!("Unknown module '{name}'"))?;
            println!("Module '{name}' will run these commands on this machine:");
            for cmd in manifest.install_commands() {
                println!("  {cmd}");
            }
            if !yes {
                print!("Continue? [y/N]: ");
                io::stdout().flush()?;
                let mut buf = String::new();
                io::stdin().read_line(&mut buf)?;
                if !matches!(buf.trim().to_lowercase().as_str(), "y" | "yes") {
                    println!("Aborted.");
                    return Ok(());
                }
            }
            reg.install(name)?;
            println!(
                "Module '{name}' installed. Set `modules.enabled: true` in the config to use it."
            );
        }
    }
    Ok(())
}

fn module_state_path() -> PathBuf {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".eullm-agent")
        .join("module-state.json")
}

/// First start of the local app without a config: use an EuLLM Engine
/// running on this computer if there is one, otherwise ask.
async fn first_local_config(path: &std::path::Path) -> Result<Config> {
    let base_url = "http://localhost:11434";
    let Some(model) = setup::detect_engine(base_url).await else {
        return wizard::run(path);
    };
    println!("\nFound an EuLLM Engine at {base_url} with model '{model}'.");
    let config = wizard::new_config(
        path,
        ProviderConfig::Eullm {
            base_url: base_url.into(),
            model,
            api_key: None,
            api_key_env: None,
        },
        None,
    );
    wizard::save(path, &config)?;
    println!("Config saved to '{}'.", path.display());
    Ok(config)
}

/// Best effort: the link is printed anyway.
fn open_browser(url: &str) {
    #[cfg(target_os = "macos")]
    let cmd = std::process::Command::new("open").arg(url).spawn();
    #[cfg(target_os = "windows")]
    let cmd = std::process::Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn();
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let cmd = std::process::Command::new("xdg-open").arg(url).spawn();
    if let Err(e) = cmd {
        warn!("could not open the browser ({e}); open the link above");
    }
}
